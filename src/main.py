#!/usr/bin/env python
#
# Copyright (C) 2020 Erlend Ekern <dev@ekern.me>
#
# Distributed under terms of the MIT license.

"""

"""
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from timeit import default_timer as timer
import os
import logging
import json
import urllib
import boto3
from datetime import datetime
from urllib import request

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Deployment Frequency: Average number of executions that successfully run through `Deploy Prod`
# Change Failure Rate: Percentage of production deployments that have failed
# Mean Time to Recovery: Average time it takes to go from a failed to a successful production deployment
# Lead Time: Average time for successful production deployments (entire execution). for Time for CircleCI + Step Functions
# TODO: Integrate CircleCI execution time into lead time (different repos with different build times can all trigger the pipeline)
# TODO: Metrics on failing Integration Tests, Smoke Tests?
# TODO: Metrics on Pipeline aborted
# TODO: Metrics on Pipeline timed out


def find_event_by_backtracking(initial_event, events, condition_fn):
    """Backtracks to the first event that matches a specific condition and returns that event"""
    event = initial_event
    for _ in range(len(events)):
        if condition_fn(event):
            return event
        event = next(
            (e for e in events if e["id"] == event["previousEventId"]), None
        )
        if event is None:
            break
    return event


def has_entered_state(state, events):
    """Check if a given state has been entered at some point during an execution"""
    return bool(
        next(
            (
                e
                for e in events
                if e["type"].endswith("StateEntered")
                and e["stateEnteredEventDetails"]["name"] == state
            ),
            None,
        )
    )


def error_cause_contains(text, events):
    """Checks if a given string exists in the error cause of an execution"""
    fail_event = next(
        (e for e in events if e["type"] == "ExecutionFailed"), {}
    )
    details = fail_event.get("executionFailedEventDetails", {})
    cause = details.get("cause", "")
    if not (fail_event or details or cause):
        return False
    return text in cause


def get_detailed_execution(execution, limit=500, client=None):
    """Returns an execution augmented with its execution history"""
    if client is None:
        client = boto3.client("stepfunctions")
    retries = 0
    while retries < 3:
        try:
            logger.debug(
                "Trying to get execution history of '%s'",
                execution["executionArn"],
            )
            events = client.get_execution_history(
                executionArn=execution["executionArn"],
                maxResults=limit,
                reverseOrder=True,
            )
            break
        except:
            if retries == 2:
                logger.exception()
                raise
            logger.warn(
                "Failed to get execution history of '%s', retrying ...",
                execution["executionArn"],
            )
            retries += 1
    return {**execution, **events}


def get_detailed_executions(state_machine_arn, limit=100, client=None):
    """Returns a list of detailed executions sorted by the start date in descending order (i.e., newest execution first)"""
    if client is None:
        client = boto3.client("stepfunctions")
    start = timer()
    executions = client.list_executions(
        stateMachineArn=state_machine_arn, maxResults=limit,
    )["executions"]
    end = timer()
    logger.info(f"Took %s s to list %s executions", end - start, limit)
    results = []
    start = timer()
    with PoolExecutor(max_workers=4) as executor:
        for res in executor.map(
            lambda e: get_detailed_execution(e, client=client), executions
        ):
            results.append(res)
    end = timer()
    logger.info(
        f"Took %s s to get execution history of %s executions in parallel",
        end - start,
        limit,
    )
    results = sorted(results, key=lambda e: e["startDate"], reverse=True)
    return results


def lambda_handler(event, context):
    logger.info("Lambda triggered with event '%s'", event)

    region = os.environ["AWS_REGION"]
    metric_namespace = os.environ["METRIC_NAMESPACE"]
    metric_dimension = os.environ["METRIC_DIMENSION"]
    status = event["detail"]["status"]
    state_machine_arn = event["detail"]["stateMachineArn"]
    execution_arn = event["detail"]["executionArn"]
    timestamp = event["time"]
    state_machine_name = state_machine_arn.split(":")[6]
    execution_name = execution_arn.split(":")[7]

    client = boto3.client("stepfunctions")
    response = client.get_execution_history(
        executionArn=execution_arn, maxResults=500, reverseOrder=True
    )
    events = response["events"]
    last_event = max(events, key=lambda e: e["id"])
    attempted_deploy_prod = has_entered_state("Deploy Prod", events)

    metrics = []

    if status == "SUCCEEDED" and attempted_deploy_prod:
        logger.info(
            "Current execution was a successful deployment to prod - need to check if a potential previous broken state has been restored by current execution"
        )
        # TODO: Filter out executions that may have started after the one this Lambda is working with
        detailed_executions = get_detailed_executions(
            state_machine_arn, client=client
        )
        detailed_executions = list(
            filter(
                lambda e: has_entered_state("Deploy Prod", e["events"])
                and e["status"] in ["SUCCEEDED", "FAILED"]
                and e["executionArn"] != execution_arn
                and not error_cause_contains(
                    "Terraform acquires a state lock", e["events"]
                ),
                detailed_executions,
            )
        )
        relevant_previous_execution = (
            detailed_executions[0] if len(detailed_executions) else None
        )
        pipeline_fixed_by_current_execution = (
            relevant_previous_execution["status"] == "FAILED"
            if relevant_previous_execution
            else False
        )
        if pipeline_fixed_by_current_execution:
            # We have found one failed deployment to prod, but there might be failed deployments that predate it
            logger.info(
                "A previous failed deployment to production has been recovered by the current execution"
            )
            # Check if there is an earlier failed execution
            index_of_earliest_failed_event = next(
                (
                    i - 1
                    for i, execution in enumerate(detailed_executions)
                    if execution["status"] == "SUCCEEDED"
                    and execution["executionArn"]
                    != relevant_previous_execution["executionArn"]
                ),
                0,
            )
            logger.info(
                "The earliest failed deployment to production was determined to be during execution '%s'",
                detailed_executions[index_of_earliest_failed_event],
            )
            initial_fail_time = detailed_executions[
                index_of_earliest_failed_event
            ]["stopDate"]
            mean_time_to_recovery = event["detail"]["stopDate"] - int(
                initial_fail_time.timestamp() * 1000
            )
            metrics.append(
                {
                    "MetricName": "MeanTimeToRecovery",
                    "Dimensions": [
                        {
                            "Name": metric_dimension,
                            "Value": state_machine_name,
                        },
                    ],
                    "Timestamp": timestamp,
                    "Value": mean_time_to_recovery,
                    "Unit": "Milliseconds",
                }
            )
        else:
            logger.info(
                "Did not find any unrecovered failed deployments to production"
            )

    if attempted_deploy_prod:
        logger.info("Current execution attempted to deploy to prod")
        if status == "SUCCEEDED":
            metrics.append(
                {
                    "MetricName": "SuccessfulDeploymentToProduction",
                    "Dimensions": [
                        {
                            "Name": metric_dimension,
                            "Value": state_machine_name,
                        },
                    ],
                    "Timestamp": timestamp,
                    "Value": 1,
                    "Unit": "Count",
                }
            )
        elif status == "FAILED" and not error_cause_contains(
            "Terraform acquires a stack lock", [last_event]
        ):
            metrics.append(
                {
                    "MetricName": "FailedDeploymentToProduction",
                    "Dimensions": [
                        {
                            "Name": metric_dimension,
                            "Value": state_machine_name,
                        },
                    ],
                    "Timestamp": timestamp,
                    "Value": 1,
                    "Unit": "Count",
                }
            )
        start_time = event["detail"]["startDate"]
        end_time = event["detail"]["stopDate"]
        if status == "SUCCEEDED" and start_time and end_time:
            duration = end_time - start_time
            metrics.append(
                {
                    "MetricName": "ExecutionTime",
                    "Dimensions": [
                        {
                            "Name": metric_dimension,
                            "Value": state_machine_name,
                        },
                    ],
                    "Timestamp": timestamp,
                    "Value": duration,
                    "Unit": "Milliseconds",
                }
            )

    if len(metrics):
        logger.info("Sending metrics to CloudWatch '%s'", metrics)
        cloudwatch_client = boto3.client("cloudwatch")
        response = cloudwatch_client.put_metric_data(
            Namespace=metric_namespace, MetricData=metrics,
        )
        logger.info("Response from putting metric '%s'", response)
    else:
        logger.info("No metrics to send to CloudWatch")
