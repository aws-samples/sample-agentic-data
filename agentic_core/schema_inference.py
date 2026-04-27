"""
Schema Inference Engine — 自动从数据文件推断语义层定义。

上传 CSV/JSON → 自动生成:
1. 数据源注册 (CHATBI_DATASETS)
2. 语义层指标 (METRICS)
3. 语义层维度 (DIMENSIONS)
4. 同义词 (SYNONYMS)
5. Agent 工具提示更新

支持: CSV, JSON (array of objects), Excel (.xlsx)
"""
import json, csv, io, os, re
from collections import Counter, defaultdict
from datetime import datetime


# ═══════ 字段类型推断 ═══════

def _infer_type(values, field_name=""):
    """Infer field type from sample values."""
    non_null = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_null:
        return "unknown"
    
    sample = non_null[:200]  # Sample up to 200
    
    # Check numeric
    num_count = 0
    float_count = 0
    for v in sample:
        s = str(v).strip().replace(",", "")
        try:
            float(s)
            num_count += 1
            if "." in s:
                float_count += 1
        except Exception:
            pass
    
    if num_count > len(sample) * 0.85:
        return "float" if float_count > len(sample) * 0.3 else "integer"
    
    # Check date
    date_patterns = [
        r'\d{4}-\d{2}-\d{2}',
        r'\d{4}/\d{2}/\d{2}',
        r'\d{2}-\d{2}-\d{4}',
    ]
    date_count = sum(1 for v in sample if any(re.match(p, str(v).strip()) for p in date_patterns))
    if date_count > len(sample) * 0.7:
        return "date"
    
    # Check boolean
    bool_vals = {"true", "false", "yes", "no", "是", "否", "1", "0", "成功", "失败"}
    if all(str(v).strip().lower() in bool_vals for v in sample):
        return "boolean"
    
    # Check categorical (low cardinality text)
    unique = set(str(v) for v in sample)
    if len(unique) <= min(20, len(sample) * 0.3):
        return "categorical"
    
    return "text"


def _detect_id_field(field_name, values):
    """Detect if a field is likely an ID/key field."""
    name_lower = field_name.lower()
    # Explicit ID patterns
    id_patterns = ["_id", "vin", "uuid", "key", "code", "编号"]
    # Must match as suffix or standalone, not substring
    if any(name_lower.endswith(p) or name_lower == p or name_lower.startswith(p + "_") for p in id_patterns):
        return True
    if name_lower == "id":
        return True
    # Exclude known non-ID fields
    non_id = ["mileage", "distance", "age", "score", "price", "cost", "count",
              "rate", "amount", "percent", "speed", "weight", "height",
              "date", "time", "name", "driver", "plate", "warranty"]
    if any(p in name_lower for p in non_id):
        return False
    # High cardinality text/integer: likely an ID
    unique = set(str(v) for v in values if v)
    if len(unique) > len(values) * 0.95 and len(values) > 10:
        return True
    return False


def _detect_metric_field(field_name, field_type, values):
    """Detect if a numeric field is likely a metric (aggregatable)."""
    if field_type not in ("integer", "float"):
        return False
    name_lower = field_name.lower()
    # Skip ID-like numeric fields
    if any(p in name_lower for p in ["_id", "id", "code", "编号", "year", "月", "日"]):
        return False
    return True


def _detect_dimension_field(field_name, field_type, values):
    """Detect if a field is a good dimension for grouping."""
    if field_type in ("categorical", "boolean"):
        return True
    if field_type == "text":
        unique = set(str(v) for v in values if v)
        if 2 <= len(unique) <= 30:
            return True
    return False


# ═══════ 中文名称生成 ═══════

_COMMON_TRANSLATIONS = {
    "vin": "VIN", "model": "车型", "city": "城市", "name": "名称",
    "type": "类型", "status": "状态", "date": "日期", "time": "时间",
    "price": "价格", "cost": "费用", "amount": "金额", "total": "总计",
    "count": "数量", "score": "评分", "rate": "比率", "ratio": "比例",
    "avg": "平均", "sum": "合计", "max": "最大", "min": "最小",
    "duration": "时长", "distance": "距离", "speed": "速度",
    "age": "年龄", "gender": "性别", "level": "等级", "category": "类别",
    "channel": "渠道", "source": "来源", "region": "区域",
    "revenue": "营收", "profit": "利润", "discount": "优惠",
    "satisfaction": "满意度", "rating": "评级", "feedback": "反馈",
    "success": "成功", "failure": "失败", "error": "错误",
    "version": "版本", "platform": "平台", "device": "设备",
    "user": "用户", "customer": "客户", "driver": "驾驶员",
    "mileage": "里程", "battery": "电池", "charging": "充电",
    "temperature": "温度", "humidity": "湿度", "pressure": "气压",
    "weight": "重量", "height": "高度", "width": "宽度",
    "frequency": "频率", "percent": "百分比", "percentage": "百分比",
}

def _generate_chinese_name(field_name):
    """Generate a Chinese display name for an English field name."""
    parts = re.split(r'[_\-\s]+', field_name.lower())
    translated = []
    for p in parts:
        if p in _COMMON_TRANSLATIONS:
            translated.append(_COMMON_TRANSLATIONS[p])
        else:
            translated.append(p)
    return "".join(translated) if any(is_cjk(c) for t in translated for c in t) else field_name


def is_cjk(char):
    return '\u4e00' <= char <= '\u9fff'


def _suggest_metric_aggregation(field_name, values):
    """Suggest the best aggregation for a metric field."""
    name_lower = field_name.lower()
    if any(k in name_lower for k in ["count", "数量", "次数"]):
        return "sum", f"SUM({field_name})"
    if any(k in name_lower for k in ["rate", "ratio", "percent", "比率", "百分比", "score", "评分"]):
        return "avg", f"ROUND(AVG({field_name}), 2)"
    if any(k in name_lower for k in ["price", "cost", "amount", "fee", "价格", "费用", "金额"]):
        return "avg", f"ROUND(AVG({field_name}), 2)"
    if any(k in name_lower for k in ["total", "revenue", "profit", "合计", "营收"]):
        return "sum", f"SUM({field_name})"
    # Default: avg for most numeric fields
    return "avg", f"ROUND(AVG({field_name}), 2)"


# ═══════ 主推断引擎 ═══════

def infer_schema(data, dataset_name, description=""):
    """
    Analyze data and generate full schema inference.
    
    Args:
        data: list of dicts (records)
        dataset_name: dataset identifier
        description: optional user description
        
    Returns:
        {
            "dataset": {...},       # CHATBI_DATASETS entry
            "fields": [...],        # Field analysis
            "metrics": {...},       # Suggested METRICS entries
            "dimensions": {...},    # Suggested DIMENSIONS entries
            "synonyms": {...},      # Suggested SYNONYMS
            "join_keys": [...],     # Detected join keys
            "summary": str,         # Human-readable summary
        }
    """
    if not data:
        return {"error": "No data provided"}
    
    # Analyze each field
    fields = []
    metrics = {}
    dimensions = {}
    synonyms = {}
    join_keys = []
    
    sample = data[:500]  # Analyze up to 500 records
    all_fields = set()
    for r in sample:
        all_fields.update(r.keys())
    
    for field_name in sorted(all_fields):
        values = [r.get(field_name) for r in sample]
        non_null = [v for v in values if v is not None and str(v).strip() != ""]
        
        field_type = _infer_type(values, field_name)
        is_id = _detect_id_field(field_name, non_null)
        is_metric = _detect_metric_field(field_name, field_type, non_null)
        is_dimension = _detect_dimension_field(field_name, field_type, non_null)
        
        unique_values = sorted(set(str(v) for v in non_null))[:20]
        chinese_name = _generate_chinese_name(field_name)
        
        field_info = {
            "name": field_name,
            "chinese_name": chinese_name,
            "type": field_type,
            "non_null_count": len(non_null),
            "null_count": len(values) - len(non_null),
            "unique_count": len(set(str(v) for v in non_null)),
            "sample_values": unique_values[:10],
            "is_id": is_id,
            "is_metric": is_metric,
            "is_dimension": is_dimension,
        }
        
        # Stats for numeric fields
        if field_type in ("integer", "float"):
            nums = []
            for v in non_null:
                try: nums.append(float(str(v).replace(",", "")))
                except Exception: pass
            if nums:
                field_info["min"] = min(nums)
                field_info["max"] = max(nums)
                field_info["mean"] = round(sum(nums) / len(nums), 2)
        
        fields.append(field_info)
        
        # Generate metric suggestion
        if is_metric and not is_id:
            agg_type, sql_expr = _suggest_metric_aggregation(field_name, non_null)
            agg_prefix = {"avg": "平均", "sum": "总", "count": ""}
            metric_name = f"{agg_prefix.get(agg_type, '')}{chinese_name}" if chinese_name != field_name else f"平均{field_name}"
            metrics[metric_name] = {
                "id": f"{dataset_name}_{field_name}",
                "description": f"{dataset_name} 的 {chinese_name}",
                "source": "chatbi",
                "dataset": dataset_name,
                "metric": f"{agg_type}:{field_name}",
                "unit": _guess_unit(field_name),
                "field": field_name,
            }
            # Add synonym
            if chinese_name != field_name:
                synonyms[field_name] = metric_name
        
        # Generate dimension suggestion
        if is_dimension and not is_id:
            dimensions[chinese_name] = {
                "id": field_name,
                "chatbi_field": field_name,
                "values": unique_values[:15],
            }
            # Add field name as synonym
            if chinese_name != field_name:
                synonyms[field_name] = chinese_name
        
        # Detect join keys
        if is_id:
            join_keys.append(field_name)
    
    # Generate dataset description if not provided
    if not description:
        dim_names = ", ".join(list(dimensions.keys())[:5])
        metric_names = ", ".join(list(metrics.keys())[:5])
        description = f"{dataset_name} 数据集 — {len(data)}条记录, {len(fields)}个字段"
        if dim_names:
            description += f", 维度: {dim_names}"
        if metric_names:
            description += f", 指标: {metric_names}"
    
    # Summary
    summary = (
        f"📊 **{dataset_name}** 数据分析完成\n"
        f"- 记录数: {len(data)}\n"
        f"- 字段数: {len(fields)}\n"
        f"- 识别指标: {len(metrics)} 个 ({', '.join(list(metrics.keys())[:5])})\n"
        f"- 识别维度: {len(dimensions)} 个 ({', '.join(list(dimensions.keys())[:5])})\n"
        f"- 关联字段: {', '.join(join_keys) if join_keys else '无'}\n"
        f"- 同义词: {len(synonyms)} 个"
    )
    
    return {
        "dataset": {
            "name": dataset_name,
            "key": f"chatbi/{dataset_name}.json",
            "desc": description,
            "record_count": len(data),
            "field_count": len(fields),
        },
        "fields": fields,
        "metrics": metrics,
        "dimensions": dimensions,
        "synonyms": synonyms,
        "join_keys": join_keys,
        "summary": summary,
    }


def _guess_unit(field_name):
    """Guess measurement unit from field name."""
    units = {
        "yuan": "元", "rmb": "元", "cny": "元", "price": "元", "cost": "元",
        "fee": "元", "amount": "元", "revenue": "元", "profit": "元",
        "km": "km", "mileage": "km", "distance": "km", "mile": "英里",
        "kwh": "kWh", "wh": "Wh", "percent": "%", "rate": "%", "ratio": "%",
        "score": "分", "rating": "分", "nps": "分",
        "second": "秒", "minute": "分钟", "hour": "小时", "day": "天",
        "celsius": "°C", "temperature": "°C", "temp": "°C",
        "kg": "kg", "weight": "kg", "ton": "吨",
        "count": "个", "num": "个", "times": "次",
        "speed": "km/h",
    }
    lower = field_name.lower()
    for k, v in units.items():
        if k in lower:
            return v
    return ""


# ═══════ 文件解析 ═══════

def parse_upload(content, filename):
    """
    Parse uploaded file content into list of dicts.
    
    Args:
        content: bytes or string
        filename: original filename
        
    Returns:
        (data: list[dict], error: str|None)
    """
    ext = os.path.splitext(filename)[1].lower()
    
    if ext == ".json":
        try:
            if isinstance(content, bytes):
                content = content.decode("utf-8-sig")
            data = json.loads(content)
            if isinstance(data, list) and data and isinstance(data[0], dict):
                return data, None
            elif isinstance(data, dict):
                # Try to find array in dict values
                for v in data.values():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v, None
                return None, "JSON 格式不支持: 需要 [{...}, ...] 数组格式"
            return None, "JSON 格式不支持: 需要对象数组"
        except json.JSONDecodeError as e:
            return None, f"JSON 解析失败: {e}"
    
    elif ext == ".csv":
        try:
            if isinstance(content, bytes):
                # Try different encodings
                for enc in ["utf-8-sig", "utf-8", "gbk", "gb2312", "latin-1"]:
                    try:
                        content = content.decode(enc)
                        break
                    except Exception:
                        continue
            reader = csv.DictReader(io.StringIO(content))
            data = list(reader)
            if not data:
                return None, "CSV 为空"
            return data, None
        except Exception as e:
            return None, f"CSV 解析失败: {e}"
    
    elif ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content if isinstance(content, bytes) else content.encode()), read_only=True)
            ws = wb.active
            rows = list(ws.iter_rows(values_only=True))
            if len(rows) < 2:
                return None, "Excel 数据不足 (至少需要表头+1行数据)"
            headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
            data = []
            for row in rows[1:]:
                record = {}
                for i, val in enumerate(row):
                    if i < len(headers):
                        if isinstance(val, datetime):
                            val = val.strftime("%Y-%m-%d")
                        record[headers[i]] = val
                data.append(record)
            return data, None
        except ImportError:
            return None, "需要安装 openpyxl: pip install openpyxl"
        except Exception as e:
            return None, f"Excel 解析失败: {e}"
    
    elif ext in (".jsonl", ".ndjson"):
        # JSONL: one JSON object per line
        try:
            if isinstance(content, bytes):
                content = content.decode("utf-8-sig")
            data = []
            for line in content.strip().split("\n"):
                line = line.strip()
                if line:
                    data.append(json.loads(line))
            if not data:
                return None, "JSONL 为空"
            return data, None
        except Exception as e:
            return None, f"JSONL 解析失败: {e}"

    elif ext == ".tsv":
        try:
            if isinstance(content, bytes):
                for enc in ["utf-8-sig", "utf-8", "gbk", "latin-1"]:
                    try:
                        content = content.decode(enc)
                        break
                    except Exception:
                        continue
            reader = csv.DictReader(io.StringIO(content), delimiter="\t")
            data = list(reader)
            if not data:
                return None, "TSV 为空"
            return data, None
        except Exception as e:
            return None, f"TSV 解析失败: {e}"

    elif ext == ".parquet":
        try:
            import pyarrow.parquet as pq
            table = pq.read_table(io.BytesIO(content if isinstance(content, bytes) else content.encode()))
            if table.num_rows == 0:
                return None, "Parquet 为空"
            # Sample max 500 rows, pure pyarrow (no pandas needed)
            if table.num_rows > 500:
                table = table.slice(0, 500)
            columns = table.column_names
            data = []
            for i in range(table.num_rows):
                record = {}
                for col in columns:
                    v = table.column(col)[i].as_py()
                    record[col] = v
                data.append(record)
            return data, None
        except ImportError:
            return None, "需要安装 pyarrow: pip install pyarrow"
        except Exception as e:
            return None, f"Parquet 解析失败: {e}"

    elif ext == ".orc":
        try:
            import pyarrow.orc as orc
            table = orc.read_table(io.BytesIO(content if isinstance(content, bytes) else content.encode()))
            if table.num_rows == 0:
                return None, "ORC 为空"
            if table.num_rows > 500:
                table = table.slice(0, 500)
            columns = table.column_names
            data = []
            for i in range(table.num_rows):
                record = {}
                for col in columns:
                    v = table.column(col)[i].as_py()
                    record[col] = v
                data.append(record)
            return data, None
        except ImportError:
            return None, "需要安装 pyarrow: pip install pyarrow"
        except Exception as e:
            return None, f"ORC 解析失败: {e}"

    else:
        return None, f"不支持的文件格式: {ext} (支持: .json, .jsonl, .csv, .tsv, .parquet, .orc, .xlsx)"


# ═══════ Data Source Auto-Discovery ═══════


def _resolve_boto3_kwargs(region, access_key="", secret_key="", role_arn="", external_id=""):
    """Build boto3 kwargs supporting platform / AssumeRole / AK-SK."""
    kwargs = {"region_name": region}
    if role_arn:
        import boto3 as _b3
        sts = _b3.client("sts", region_name=region)
        params = {"RoleArn": role_arn, "RoleSessionName": "agentic-data-introspect", "DurationSeconds": 3600}
        if external_id:
            params["ExternalId"] = external_id
        creds = sts.assume_role(**params)["Credentials"]
        kwargs["aws_access_key_id"] = creds["AccessKeyId"]
        kwargs["aws_secret_access_key"] = creds["SecretAccessKey"]
        kwargs["aws_session_token"] = creds["SessionToken"]
    elif access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    return kwargs

def introspect_dynamodb(table_name, region="us-east-1", sample_limit=100, access_key="", secret_key="", role_arn="", external_id=""):
    """Introspect a DynamoDB table → field analysis + semantic suggestions."""
    import boto3
    kwargs = _resolve_boto3_kwargs(region, access_key, secret_key, role_arn, external_id)
    ddb = boto3.resource("dynamodb", **kwargs)
    table = ddb.Table(table_name)
    resp = table.scan(Limit=sample_limit)
    items = resp.get("Items", [])
    if not items:
        return {"error": f"表 {table_name} 为空或无法访问"}
    # Convert Decimal to float
    import decimal
    clean = []
    for item in items:
        row = {}
        for k, v in item.items():
            if isinstance(v, decimal.Decimal):
                v = float(v)
            row[k] = v
        clean.append(row)
    result = infer_schema(clean, table_name.replace("-", "_"))
    result["source_type"] = "DynamoDB"
    result["source_config"] = {"table": table_name, "region": region}
    result["dataset"]["source"] = "dynamodb"
    return result


def introspect_s3_json(bucket, key, region="us-east-1", access_key="", secret_key="", role_arn="", external_id=""):
    """Introspect any S3 file (JSON/CSV/Parquet/JSONL/ORC/Excel)."""
    import boto3
    kwargs = _resolve_boto3_kwargs(region, access_key, secret_key, role_arn, external_id)
    s3 = boto3.client("s3", **kwargs)
    return _introspect_s3_file(s3, bucket, key, region, access_key, secret_key, role_arn, external_id)


SUPPORTED_S3_EXTS = {".json", ".jsonl", ".ndjson", ".csv", ".tsv", ".parquet", ".orc", ".xlsx"}

def introspect_s3_prefix(bucket, prefix, region="us-east-1", access_key="", secret_key="", role_arn="", external_id=""):
    """Scan S3 prefix for data files (JSON/JSONL/CSV/TSV/Parquet/ORC/Excel).
    
    Smart behavior:
    - If prefix points to a single file → introspect that file
    - If prefix is a directory → scan files, group by directory as datasets
    - Same-directory Parquet/CSV files are merged as one dataset
    - Max 100MB per file, max 50 files per scan
    """
    import boto3
    kwargs = _resolve_boto3_kwargs(region, access_key, secret_key, role_arn, external_id)
    s3 = boto3.client("s3", **kwargs)
    
    # Normalize prefix
    if not prefix:
        prefix = ""
    
    # Check if prefix points to a single file
    ext = os.path.splitext(prefix)[1].lower()
    if ext in SUPPORTED_S3_EXTS:
        try:
            r = _introspect_s3_file(s3, bucket, prefix, region, access_key, secret_key, role_arn, external_id)
            return [r] if r and "error" not in r else []
        except Exception:
            return []
    
    # Directory scan
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    
    paginator = s3.get_paginator("list_objects_v2")
    files_by_dir = {}  # {dir_path: [(key, ext, size), ...]}
    
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix, PaginationConfig={"MaxItems": 200}):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            size = obj["Size"]
            fext = os.path.splitext(key)[1].lower()
            if fext not in SUPPORTED_S3_EXTS or size > 100 * 1024 * 1024 or size == 0:
                continue
            # Group by parent directory
            dir_path = os.path.dirname(key)
            files_by_dir.setdefault(dir_path, []).append((key, fext, size))
    
    results = []
    for dir_path, files in files_by_dir.items():
        # Group files by format
        by_fmt = {}
        for key, fext, size in files:
            by_fmt.setdefault(fext, []).append((key, size))
        
        for fext, file_list in by_fmt.items():
            if fext in (".parquet", ".orc"):
                # Columnar: sample first file only (they share schema)
                key, _ = file_list[0]
                try:
                    r = _introspect_s3_file(s3, bucket, key, region, access_key, secret_key, role_arn, external_id)
                    if r and "error" not in r:
                        r["file_count"] = len(file_list)
                        r["total_size"] = sum(s for _, s in file_list)
                        # Use table directory name (skip Hive partition dirs like dt=xxx)
                        parts = dir_path.split("/")
                        # Walk up until we find a non-partition dir
                        dir_name = ""
                        for p in reversed(parts):
                            if "=" not in p and p:  # skip dt=2026-03-07 style
                                dir_name = p
                                break
                        if not dir_name:
                            dir_name = os.path.basename(key).rsplit(".", 1)[0]
                        r["dataset"]["name"] = dir_name.replace("-", "_")
                        results.append(r)
                except Exception:
                    pass
            else:
                # Row-oriented (JSON/CSV/JSONL): introspect each file
                for key, size in file_list[:10]:  # Max 10 files per format per dir
                    try:
                        r = _introspect_s3_file(s3, bucket, key, region, access_key, secret_key, role_arn, external_id)
                        if r and "error" not in r:
                            results.append(r)
                    except Exception:
                        pass
    
    return results


def _introspect_s3_file(s3_client, bucket, key, region="us-east-1", access_key="", secret_key="", role_arn="", external_id=""):
    """Introspect a single S3 file of any supported format."""
    ext = os.path.splitext(key)[1].lower()
    
    # For large files, only read partial content
    size_resp = s3_client.head_object(Bucket=bucket, Key=key)
    file_size = size_resp["ContentLength"]
    
    # Parquet/ORC: need full file (columnar format)
    # Others: cap at 10MB for sampling
    if ext in (".parquet", ".orc"):
        max_bytes = min(file_size, 50 * 1024 * 1024)  # 50MB max
    else:
        max_bytes = min(file_size, 10 * 1024 * 1024)  # 10MB max
    
    if max_bytes < file_size:
        obj = s3_client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes-1}")
    else:
        obj = s3_client.get_object(Bucket=bucket, Key=key)
    
    content = obj["Body"].read()
    
    # Use filename as hint for parse_upload
    data, err = parse_upload(content, key)
    if err:
        return {"error": err}
    
    # Smart naming: for partitioned data, use table dir name; for single file, use filename
    basename = os.path.basename(key)
    name_parts = key.rstrip("/").split("/")
    ds_name = ""
    for p in reversed(name_parts[:-1]):  # skip the filename itself
        if "=" not in p and p:  # skip Hive partition dirs
            ds_name = p
            break
    if not ds_name:
        ds_name = os.path.splitext(basename)[0]
    # Remove .snappy or similar compression suffix
    ds_name = ds_name.replace(".snappy", "").replace(".gzip", "").replace(".zstd", "")
    ds_name = ds_name.replace("-", "_")
    result = infer_schema(data, ds_name)
    result["source_type"] = "S3"
    result["source_config"] = {"bucket": bucket, "key": key, "region": region, "format": ext}
    result["file_size"] = file_size
    return result


def introspect_athena(database, region="us-east-1", access_key="", secret_key="", role_arn="", external_id=""):
    """Introspect Athena database → table list + column schemas."""
    import boto3
    kwargs = _resolve_boto3_kwargs(region, access_key, secret_key, role_arn, external_id)
    ath = boto3.client("athena", **kwargs)
    resp = ath.list_table_metadata(
        CatalogName="AwsDataCatalog",
        DatabaseName=database,
        MaxResults=20
    )
    results = []
    for table_meta in resp.get("TableMetadataList", []):
        table_name = table_meta["Name"]
        columns = table_meta.get("Columns", [])
        partitions = table_meta.get("PartitionKeys", [])
        
        fields = []
        metrics = {}
        dimensions = {}
        
        for col in columns + partitions:
            col_name = col["Name"]
            col_type = col.get("Type", "string")
            chinese_name = _generate_chinese_name(col_name)
            
            is_numeric = col_type in ("int", "bigint", "double", "float", "decimal")
            is_dim = col_type in ("string", "varchar", "char", "boolean")
            is_partition = col in partitions
            
            field_info = {
                "name": col_name,
                "chinese_name": chinese_name,
                "type": "float" if col_type in ("double", "float", "decimal") else 
                        "integer" if col_type in ("int", "bigint") else
                        "date" if "date" in col_type or "timestamp" in col_type else
                        "text",
                "non_null_count": -1,  # Can't know without querying
                "is_id": _detect_id_field(col_name, []),
                "is_metric": is_numeric and not _detect_id_field(col_name, []),
                "is_dimension": is_dim or is_partition,
                "is_partition": is_partition,
            }
            fields.append(field_info)
            
            if field_info["is_metric"]:
                agg = "avg" if col_type in ("double", "float", "decimal") else "sum"
                metric_name = f"平均{chinese_name}" if agg == "avg" else f"总{chinese_name}"
                metrics[metric_name] = {
                    "id": f"athena_{database}_{table_name}_{col_name}",
                    "description": f"{table_name}.{col_name}",
                    "sql": f"{'ROUND(AVG' if agg=='avg' else 'SUM'}({col_name}{')'if agg=='avg' else ''}{', 2)' if agg=='avg' else ')'}",
                    "source": "athena",
                    "table": f"{database}.{table_name}",
                    "unit": _guess_unit(col_name),
                }
            
            if field_info["is_dimension"]:
                dimensions[chinese_name] = {
                    "id": col_name,
                    "athena_column": col_name,
                    "table": f"{database}.{table_name}",
                }
        
        results.append({
            "dataset": {
                "name": f"{database}_{table_name}",
                "desc": f"Athena {database}.{table_name} — {len(columns)}列{'+' + str(len(partitions)) + '分区' if partitions else ''}",
                "field_count": len(fields),
            },
            "fields": fields,
            "metrics": metrics,
            "dimensions": dimensions,
            "synonyms": {},
            "join_keys": [f["name"] for f in fields if f["is_id"]],
            "source_type": "Athena",
            "source_config": {"database": database, "table": table_name, "region": region},
            "summary": f"📊 **{database}.{table_name}** — {len(metrics)} 指标, {len(dimensions)} 维度",
        })
    
    return results


def introspect_sql_engine(engine):
    """Introspect a SQL engine (SQLite/Snowflake/PostgreSQL) → schemas."""
    schema = engine.get_schema()
    results = []
    
    for table_name, table_schema in schema.items():
        columns = table_schema.get("columns", [])
        sample = table_schema.get("sample", [])
        row_count = table_schema.get("row_count", 0)
        
        # Build pseudo data for inference
        if sample:
            data = sample
        else:
            # Build from column info
            data = [{col["name"]: None for col in columns}]
        
        fields = []
        metrics = {}
        dimensions = {}
        
        for col in columns:
            col_name = col["name"]
            col_type_raw = col.get("type", "TEXT").upper()
            chinese_name = _generate_chinese_name(col_name)
            
            is_numeric = any(t in col_type_raw for t in ["INT", "REAL", "FLOAT", "DOUBLE", "DECIMAL", "NUM"])
            is_text = any(t in col_type_raw for t in ["TEXT", "VARCHAR", "CHAR", "STRING"])
            is_date = any(t in col_type_raw for t in ["DATE", "TIME", "STAMP"])
            
            # Use sample data for better inference if available
            if sample:
                values = [r.get(col_name) for r in sample]
                inferred = _infer_type(values, col_name)
                is_id = _detect_id_field(col_name, values)
                unique_vals = sorted(set(str(v) for v in values if v is not None))[:15]
            else:
                inferred = "float" if is_numeric else "date" if is_date else "text"
                is_id = _detect_id_field(col_name, [])
                unique_vals = []
            
            is_metric_f = is_numeric and not is_id
            is_dim_f = (is_text or len(unique_vals) <= 20) and not is_id and not is_numeric
            
            fields.append({
                "name": col_name,
                "chinese_name": chinese_name,
                "type": inferred,
                "sql_type": col_type_raw,
                "is_id": is_id,
                "is_metric": is_metric_f,
                "is_dimension": is_dim_f,
                "sample_values": unique_vals[:10],
            })
            
            if is_metric_f:
                agg = "avg"
                for k in ["count", "数量", "total", "sum"]:
                    if k in col_name.lower():
                        agg = "sum"
                        break
                metric_name = f"{'平均' if agg=='avg' else '总'}{chinese_name}"
                metrics[metric_name] = {
                    "id": f"sql_{table_name}_{col_name}",
                    "description": f"{engine.name}.{table_name}.{col_name}",
                    "sql": f"ROUND(AVG({col_name}), 2)" if agg == "avg" else f"SUM({col_name})",
                    "source": "rds",
                    "table": table_name,
                    "unit": _guess_unit(col_name),
                }
            
            if is_dim_f:
                dimensions[chinese_name] = {
                    "id": col_name,
                    "rds_column": col_name,
                    "values": unique_vals[:15],
                }
        
        results.append({
            "dataset": {
                "name": table_name,
                "desc": f"{engine.name}.{table_name} — {row_count}行, {len(columns)}列",
                "record_count": row_count,
                "field_count": len(fields),
            },
            "fields": fields,
            "metrics": metrics,
            "dimensions": dimensions,
            "synonyms": {},
            "join_keys": [f["name"] for f in fields if f["is_id"]],
            "source_type": f"SQL ({engine.dialect})",
            "source_config": {"engine": engine.name, "table": table_name},
            "summary": f"📊 **{table_name}** ({engine.dialect}) — {row_count}行, {len(metrics)} 指标, {len(dimensions)} 维度",
        })
    
    return results


def introspect_tableau(client):
    """Introspect Tableau workbooks/views → semantic suggestions."""
    results = []
    try:
        workbooks = client.list_workbooks()
        for wb in workbooks:
            for view in wb.get("views", []):
                view_name = view.get("name", "")
                fields_info = client.get_view_fields(view.get("id", ""))
                
                fields = []
                metrics = {}
                dimensions = {}
                
                for f in fields_info.get("fields", []):
                    fname = f.get("name", "")
                    ftype = f.get("role", "dimension")  # measure or dimension
                    chinese_name = _generate_chinese_name(fname)
                    
                    if ftype == "measure":
                        metrics[f"Tableau {chinese_name}"] = {
                            "id": f"tableau_{view_name}_{fname}",
                            "description": f"Tableau 报表 {wb['name']}.{view_name} 的 {fname}",
                            "source": "tableau",
                            "view": view_name,
                            "unit": _guess_unit(fname),
                        }
                    else:
                        dimensions[chinese_name] = {
                            "id": fname,
                            "tableau_field": fname,
                            "view": view_name,
                        }
                    
                    fields.append({
                        "name": fname,
                        "chinese_name": chinese_name,
                        "type": "float" if ftype == "measure" else "categorical",
                        "is_metric": ftype == "measure",
                        "is_dimension": ftype == "dimension",
                    })
                
                results.append({
                    "dataset": {
                        "name": f"tableau_{view_name}",
                        "desc": f"Tableau {wb['name']}/{view_name}",
                        "field_count": len(fields),
                    },
                    "fields": fields,
                    "metrics": metrics,
                    "dimensions": dimensions,
                    "synonyms": {},
                    "join_keys": [],
                    "source_type": "Tableau",
                    "source_config": {"workbook": wb["name"], "view": view_name},
                    "summary": f"📊 **Tableau {view_name}** — {len(metrics)} 指标, {len(dimensions)} 维度",
                })
    except Exception as e:
        return [{"error": str(e)}]
    
    return results
