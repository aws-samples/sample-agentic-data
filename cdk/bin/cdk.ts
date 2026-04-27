#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { AgenticDataStack } from '../lib/agentic-data-stack';

const app = new cdk.App();

const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: process.env.CDK_DEFAULT_REGION || 'us-east-1',
};

// Auto-detect China region for stack naming
const isChina = (env.region || '').startsWith('cn-');
const stackName = isChina ? 'AgenticDataStack-CN' : 'AgenticDataStack';

new AgenticDataStack(app, stackName, {
  env,
  description: `Agentic Data - Multi-Agent Data Analytics Platform${isChina ? ' (China)' : ''}`,
});
