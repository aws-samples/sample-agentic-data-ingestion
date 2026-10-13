# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
AgentCore Runtime entrypoint for Glue pipeline creation agent.
"""

import logging
import os
import sys

# P28 R14.3 recursion guard: this container vendors the Lambda handler, whose
# proxy path is gated on USE_AGENTCORE_RUNTIME. The flag must never be visible
# inside the container — if it leaked into the container env, the vendored
# handler would re-enter the AgentCore proxy and recurse into this very
# runtime. Strip it at startup, before the handler can consult it.
os.environ.pop('USE_AGENTCORE_RUNTIME', None)

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s %(message)s', force=True)
for name in list(logging.Logger.manager.loggerDict):
    logging.getLogger(name).propagate = True

sys.path.insert(0, '/app/shared')
sys.path.insert(0, '/app/lambda/agentic_data_ingestion_create_glue_pipeline')

from bedrock_agentcore.runtime import BedrockAgentCoreApp

logger = logging.getLogger(__name__)
app = BedrockAgentCoreApp()


@app.entrypoint
def handler(payload: dict) -> dict:
    from lambda_function import lambda_handler

    run_id = payload.get('run_id', 'unknown')
    pipeline_id = payload.get('pipeline_id', 'unknown')

    root = logging.getLogger()
    for h in root.handlers:
        h.setFormatter(logging.Formatter(f'%(levelname)s [run_id={run_id}] %(name)s %(message)s'))
    for name in list(logging.Logger.manager.loggerDict):
        lg = logging.getLogger(name)
        lg.propagate = True
        lg.setLevel(logging.INFO)

    logger.info('create_glue_pipeline invoked for pipeline_id=%s', pipeline_id)

    # P28 R19.11 idle-proxy billing: a payload carrying a Step Functions task
    # token means the task state is waitForTaskToken — background the work
    # (add_async_task flips /ping to HealthyBusy so the microVM stays
    # scheduled) and return an immediate dispatch ack so the proxy Lambda
    # stops billing within seconds. The background thread closes the loop via
    # SendTaskSuccess/SendTaskFailure. The token is POPPED so the vendored
    # handler's own step_task_token wrapper never double-sends — this
    # entrypoint is the single callback owner inside the container.
    task_token = payload.pop('task_token', None)
    if task_token:
        from sfn_task_token import dispatch_background_step

        return dispatch_background_step(
            app=app,
            handler=lambda_handler,
            payload=payload,
            task_token=task_token,
            step_name='create_glue_pipeline',
        )

    result = lambda_handler(payload, None)
    logger.info('create_glue_pipeline completed with statusCode=%s', result.get('statusCode', 'unknown'))
    return result


if __name__ == '__main__':
    app.run()
