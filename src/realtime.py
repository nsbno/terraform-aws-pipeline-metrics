#!/usr/bin/env python
#
# Copyright (C) 2021 Vy
#
# Distributed under terms of the MIT license.

"""
A Lambda triggered by EventBridge events emitted by
AWS Step Functions execution updates.
"""

import json
import logging
import os
import boto3

from main import (
    get_state_events,
    get_names_of_entered_states,
    put_cloudwatch_metrics,
)


logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info(
        "Lambda triggered with event: %s", json.dumps(event),
    )
    metric_namespace = os.environ["METRIC_NAMESPACE"]

    state_machine_arn = event["detail"]["stateMachineArn"]
    state_machine_name = state_machine_arn.split(":")[6]

    execution_arn = event["detail"]["executionArn"]

    sfn = boto3.client("stepfunctions")
    response = sfn.get_execution_history(
        executionArn=execution_arn, maxResults=500, reverseOrder=True
    )
    events = response["events"]
    state_names = get_names_of_entered_states(events)
    logger.info(
        "%s states were entered during the execution", len(state_names)
    )
    metric_datums = []
    for state_name in state_names:
        state_events = get_state_events(state_name, events)
        if state_events["success_event"]:
            metric_datums.append(
                {
                    "MetricName": "StateSuccess",
                    "Timestamp": state_events["success_event"]["timestamp"],
                    "Dimensions": [
                        {
                            "Name": "StateMachineName",
                            "Value": state_machine_name,
                        },
                        {"Name": "StateName", "Value": state_name},
                    ],
                    "Value": int(
                        (
                            state_events["success_event"]["timestamp"]
                            - state_events["enter_event"]["timestamp"]
                        ).total_seconds()
                        * 1000
                    ),
                    "Unit": "Milliseconds",
                    "StorageResolution": 1,
                }
            )
        elif state_events["fail_event"]:
            metric_datums.append(
                {
                    "MetricName": "StateFail",
                    "Timestamp": state_events["fail_event"]["timestamp"],
                    "Dimensions": [
                        {
                            "Name": "StateMachineName",
                            "Value": state_machine_name,
                        },
                        {"Name": "StateName", "Value": state_name},
                    ],
                    "Value": int(
                        (
                            state_events["fail_event"]["timestamp"]
                            - state_events["enter_event"]["timestamp"]
                        ).total_seconds()
                        * 1000
                    ),
                    "Unit": "Milliseconds",
                    "StorageResolution": 1,
                },
            )
    logger.info(
        "Publishing %s custom metrics to CloudWatch", len(metric_datums)
    )
    put_cloudwatch_metrics(metric_datums, metric_namespace)
