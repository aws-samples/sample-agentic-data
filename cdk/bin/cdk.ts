#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { Validations } from 'aws-cdk-lib';
import { AwsSolutionsChecks } from 'cdk-nag';
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

// cdk_nag runs on every synth, so `cdk synth` is the scan -- there is no separate
// command to remember and no way to ship a change without seeing the findings.
// Suppressions live next to the construct they excuse, in agentic-data-stack.ts,
// each with a reason; `cdk synth` fails on an unsuppressed Error-level finding.
//
// Set -c skip_nag=true only to bisect a synth problem. It is not for silencing
// findings: the scan is the evidence attached to this content's PCSR.
// cdk-nag v3 is a policy validation plugin, not an Aspect; v2's
// Aspects.of(app).add(...) does not type-check against it.
if (String(app.node.tryGetContext('skip_nag') ?? 'false') !== 'true') {
  Validations.of(app).addPlugins(new AwsSolutionsChecks(app, { verbose: true }));
}
