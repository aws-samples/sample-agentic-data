"""
Orchestrator — 确定性编排引擎

4 种模式:
- direct:     单 Agent 直连，不经 Supervisor (Fast-Path, 0ms 开销)
- parallel:   多 Agent 并行执行，合并结果
- pipeline:   多 Agent 串行，前一个输出是下一个输入
- supervisor: LLM 动态路由（仅在规则无法判断时）

设计文档: docs/agent-scene-redesign-v2.md
"""
import json
import time
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Callable
from dataclasses import dataclass, field

from agentic_core.agent_registry import get_agent_def, get_scene, list_agents


@dataclass
class OrchestrationContext:
    """Shared context passed through orchestration."""
    session_id: str
    question: str
    scene_id: Optional[str] = None
    user_email: str = ""
    trace_id: str = ""
    
    # SSE callback: fn(agent_id, event_type, data)
    emit: Optional[Callable] = None
    
    # Accumulated results
    results: list = field(default_factory=list)
    total_cost: float = 0.0
    total_tokens: int = 0
    
    # Timing
    start_time: float = field(default_factory=time.time)


class Orchestrator:
    """确定性编排引擎。"""
    
    def __init__(self, agent_factory=None, supervisor_factory=None):
        """
        agent_factory: fn(agent_def, context) -> Strands Agent instance
        supervisor_factory: fn(config) -> Strands Supervisor Agent instance
        """
        self._agent_factory = agent_factory
        self._supervisor_factory = supervisor_factory
    
    def execute(self, ctx: OrchestrationContext) -> str:
        """
        Main entry point. Determines mode from scene config and dispatches.
        Returns the final response text.
        """
        scene = None
        if ctx.scene_id:
            scene = get_scene(ctx.scene_id)
        
        if not scene:
            # No scene selected → supervisor mode (legacy behavior)
            return self._mode_supervisor(ctx, agent_ids=None)
        
        orch = scene.get("orchestration", {})
        mode = orch.get("mode", "direct")
        agent_refs = orch.get("agents", [])
        
        # V1 compat: if scene has _v1 data, extract agent IDs differently
        if not agent_refs and "_v1" in scene:
            v1 = scene["_v1"]
            v1_agents = v1.get("agents", [])
            agent_refs = [{"id": f"agent_{a}", "role": "primary"} for a in v1_agents]
        
        agent_ids = [ref["id"] if isinstance(ref, dict) else ref for ref in agent_refs]
        
        if not agent_ids:
            return self._mode_supervisor(ctx, agent_ids=None)
        
        if mode == "direct":
            return self._mode_direct(ctx, agent_ids[0])
        elif mode == "parallel":
            merge = orch.get("merge_strategy", "concat")
            return self._mode_parallel(ctx, agent_ids, merge_strategy=merge)
        elif mode == "pipeline":
            return self._mode_pipeline(ctx, agent_ids)
        elif mode == "supervisor":
            return self._mode_supervisor(ctx, agent_ids)
        else:
            print(f"[Orchestrator] Unknown mode '{mode}', falling back to direct")
            return self._mode_direct(ctx, agent_ids[0])
    
    # ─── Mode: Direct (Fast-Path) ───
    
    def _mode_direct(self, ctx: OrchestrationContext, agent_id: str) -> str:
        """
        单 Agent 直连。不经过 Supervisor，0ms 编排开销。
        90% 场景的默认模式。
        """
        self._emit(ctx, "system", "agent_start", {"agent_id": agent_id, "mode": "direct"})
        
        agent_def = get_agent_def(agent_id)
        if not agent_def:
            return f"Agent {agent_id} not found"
        
        try:
            result = self._run_agent(ctx, agent_def)
            self._emit(ctx, agent_id, "agent_end", {"success": True})
            return result
        except Exception as e:
            self._emit(ctx, agent_id, "run_error", {"error": str(e)})
            traceback.print_exc()
            return f"Agent execution error: {e}"
    
    # ─── Mode: Parallel ───
    
    def _mode_parallel(self, ctx: OrchestrationContext, agent_ids: list, merge_strategy: str = "concat") -> str:
        """
        多 Agent 并行执行，合并结果。
        编排层是确定性代码，不消耗 LLM token。
        """
        self._emit(ctx, "system", "run_start", {
            "mode": "parallel",
            "agents": agent_ids,
        })
        
        results = {}
        errors = {}
        
        def _run_one(aid):
            agent_def = get_agent_def(aid)
            if not agent_def:
                return aid, None, f"Agent {aid} not found"
            try:
                r = self._run_agent(ctx, agent_def)
                return aid, r, None
            except Exception as e:
                traceback.print_exc()
                return aid, None, str(e)
        
        with ThreadPoolExecutor(max_workers=min(len(agent_ids), 4)) as executor:
            futures = {executor.submit(_run_one, aid): aid for aid in agent_ids}
            for future in as_completed(futures):
                aid, result, error = future.result()
                if error:
                    errors[aid] = error
                else:
                    results[aid] = result
        
        if not results:
            return f"All agents failed: {errors}"
        
        # Merge results
        if merge_strategy == "concat":
            merged = self._merge_concat(results)
        elif merge_strategy == "summarize":
            merged = self._merge_summarize(ctx, results)
        else:
            merged = self._merge_concat(results)
        
        self._emit(ctx, "system", "run_end", {
            "mode": "parallel",
            "agent_count": len(results),
            "error_count": len(errors),
        })
        
        return merged
    
    # ─── Mode: Pipeline ───
    
    def _mode_pipeline(self, ctx: OrchestrationContext, agent_ids: list) -> str:
        """
        串行流水线。前一个 Agent 的输出是下一个的输入。
        """
        self._emit(ctx, "system", "run_start", {
            "mode": "pipeline",
            "agents": agent_ids,
        })
        
        current_input = ctx.question
        
        for i, aid in enumerate(agent_ids):
            agent_def = get_agent_def(aid)
            if not agent_def:
                return f"Pipeline agent {aid} not found"
            
            self._emit(ctx, aid, "agent_start", {"step": i + 1, "total": len(agent_ids)})
            
            # Create a sub-context with modified question
            sub_ctx = OrchestrationContext(
                session_id=ctx.session_id,
                question=current_input,
                user_email=ctx.user_email,
                trace_id=ctx.trace_id,
                emit=ctx.emit,
            )
            
            try:
                result = self._run_agent(sub_ctx, agent_def)
                current_input = result  # Next agent gets this as input
                self._emit(ctx, aid, "agent_end", {"step": i + 1, "success": True})
            except Exception as e:
                self._emit(ctx, aid, "run_error", {"step": i + 1, "error": str(e)})
                return f"Pipeline failed at step {i + 1} ({aid}): {e}"
        
        return current_input
    
    # ─── Mode: Supervisor (LLM routing) ───
    
    def _mode_supervisor(self, ctx: OrchestrationContext, agent_ids: list | None) -> str:
        """
        LLM 动态路由。只在 mode=supervisor 且规则无法判断时使用。
        
        This falls back to the existing Supervisor Agent logic for backward compat.
        """
        self._emit(ctx, "system", "run_start", {"mode": "supervisor"})
        
        if self._supervisor_factory is None:
            return "Supervisor factory not configured"
        
        # Build config from agent definitions
        config = self._build_supervisor_config(agent_ids)
        
        # Use existing Supervisor creation path
        return self._run_supervisor(ctx, config)
    
    # ─── Agent Execution ───
    
    def _run_agent(self, ctx: OrchestrationContext, agent_def: dict) -> str:
        """
        Run a single Agent with its defined config.
        This is the core execution unit.
        """
        if self._agent_factory is None:
            raise RuntimeError("Agent factory not configured")
        
        return self._agent_factory(agent_def, ctx)
    
    def _run_supervisor(self, ctx: OrchestrationContext, config: dict) -> str:
        """Run existing Supervisor Agent (backward compat)."""
        if self._supervisor_factory is None:
            raise RuntimeError("Supervisor factory not configured")
        
        return self._supervisor_factory(config, ctx)
    
    # ─── Merge Strategies ───
    
    def _merge_concat(self, results: dict) -> str:
        """Simple concatenation of all agent results."""
        parts = []
        for aid, result in results.items():
            agent_def = get_agent_def(aid)
            name = agent_def.get("name", aid) if agent_def else aid
            parts.append(f"## {name}\n\n{result}")
        return "\n\n---\n\n".join(parts)
    
    def _merge_summarize(self, ctx: OrchestrationContext, results: dict) -> str:
        """LLM-powered summarization of parallel results."""
        # For now, fall back to concat. Full implementation uses a lightweight LLM call.
        # TODO: implement LLM merge
        return self._merge_concat(results)
    
    # ─── Config Building ───
    
    def _build_supervisor_config(self, agent_ids: list | None) -> dict:
        """Build Supervisor config from Agent definitions."""
        config = {}
        
        if not agent_ids:
            return config
        
        # Merge datasources, tools, etc from all agents
        all_tools = set()
        all_ds = []
        all_skills = []
        
        for aid in agent_ids:
            adef = get_agent_def(aid)
            if not adef:
                continue
            all_tools.update(adef.get("tools", []))
            all_ds.extend(adef.get("datasources", []))
            all_skills.extend(adef.get("skills", []))
        
        if all_tools:
            config["scenario_da_tools"] = list(all_tools)
        if all_ds:
            config["_scenario_cfg"] = {"datasources": list(set(all_ds))}
        if all_skills:
            config["skills"] = list(set(all_skills))
        
        return config
    
    # ─── Event Emission ───
    
    def _emit(self, ctx: OrchestrationContext, agent_id: str, event_type: str, data: dict):
        """Emit an SSE event if callback is configured."""
        if ctx.emit:
            try:
                ctx.emit(agent_id, event_type, data)
            except Exception as e:
                print(f"[Orchestrator] Emit error: {e}")
