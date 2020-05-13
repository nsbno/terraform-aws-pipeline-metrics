#!/usr/bin/env python
#
# Copyright (C) 2020 Erlend Ekern <dev@ekern.me>
#
# Distributed under terms of the MIT license.

"""

"""
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from timeit import default_timer as timer
import decimal
import os
import logging
import json
import urllib
import boto3
import botocore
import re
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


def get_fail_event_for_state(state, events):
    """Return the event that made a given state fail during an execution"""
    failed_event = next(
        (
            e
            for e in events
            if e["type"].endswith("Failed")
            and find_event_by_backtracking(
                e,
                events,
                lambda e2: e2["type"].endswith("StateEntered")
                and e2["stateEnteredEventDetails"]["name"] == state,
            )
        ),
        None,
    )
    if failed_event:
        # Different state types use different names for storing details about the failed event
        # taskFailedEventDetails, activityFailedEventDetails, etc.
        logger.info("State '%s failed in event '%s'", state, failed_event)
        failed_event_details_key = next(
            (
                key
                for key in failed_event
                if key.endswith("FailedEventDetails")
                and all(
                    required_key in failed_event[key]
                    for required_key in ["error", "cause"]
                )
            ),
            None,
        )
        return {
            **failed_event,
            "failedEventDetails": failed_event.get(failed_event_details_key),
        }
    return None


def has_successfully_exited_state(state, events):
    """Check if a given state has successfully exited at some point during an execution"""
    exit_event = next(
        (
            e
            for e in events
            if e["type"].endswith("StateExited")
            and e["stateExitedEventDetails"]["name"] == state
        ),
        None,
    )
    if exit_event:
        prev_event = find_event_by_backtracking(
            exit_event,
            events,
            lambda e: e["id"] == exit_event["previousEventId"]
            and e["type"].endswith("Succeeded"),
        )
        if prev_event:
            return True
    return False


def has_exited_state(state, events):
    """Check if a given state has been exited at some point during an execution"""
    return bool(
        next(
            (
                e
                for e in events
                if e["type"].endswith("StateExited")
                and e["stateExitedEventDetails"]["name"] == state
            ),
            None,
        )
    )


def error_cause_contains(text, events):
    """Check if a given string exists in the error cause of an execution"""
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
        except botocore.exceptions.ClientError:
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


def replace_special_characters(string):
    """Return a string where special characters are replaced by underscores"""
    return re.sub(r"[^a-zA-Z0-9_-]", "_", string)


def get_state_info(state_name, state_machine_name, events):
    """Return a dictionary containing various data for a given state in a given execution"""
    value = get_ssm_value_for_state(state_name, state_machine_name)
    return {
        "state_name": state_name,
        "fail_event": get_fail_event_for_state(state_name, events),
        "has_entered_state": has_entered_state(state_name, events),
        "has_successfully_exited_state": has_successfully_exited_state(
            state_name, events
        ),
        "ssm_value": value,
    }


def get_ssm_value_for_state(state_name, state_machine_name, client=None):
    """Return the JSON-formatted value of an SSM parameter used to store information for a given state of a given state machine"""
    if client is None:
        client = boto3.client("ssm")

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    try:
        response = table.get_item(
            TableName=os.environ["DYNAMODB_TABLE"],
            Key={
                "state_machine_name": state_machine_name,
                "state_name": state_name,
            },
            ConsistentRead=True,
        )
        if response.get("Item", None):
            item = response["Item"]
            # Convert Decimal to integer
            item = {
                **item,
                **{
                    k: int(v)
                    for k, v in item.items()
                    if isinstance(v, decimal.Decimal)
                },
            }
            logger.info("Found DynamoDB item '%s'", item)
        else:
            logger.info(
                "Did not find DynamoDB item, got response '%s'", response
            )

    except:
        # except dynamodb_client.exceptions.ResourceNotFoundException:
        logger.exception("Could not get DynamoDB item")

    formatted_state_name = replace_special_characters(state_name)
    parameter_name = f"/{state_machine_name}/{formatted_state_name}"
    try:
        parameter = client.get_parameter(Name=parameter_name)
        value = json.loads(parameter["Parameter"]["Value"])
        return value
    except client.exceptions.ParameterNotFound:
        return None


def set_ssm_value_for_state(
    value, state_name, state_machine_name, client=None
):
    """Set a JSON-formatted value of an SSM parameter used to store information for a given state of a given state machine"""
    if client is None:
        client = boto3.client("ssm")
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])
    try:
        json_decoded = json.loads(value)
        item = {
            "state_machine_name": state_machine_name,
            "state_name": state_name,
            **json_decoded,
        }
        logger.info("Updating DynamoDB Item '%s'", item)

        item = table.put_item(Item=item)
    except:
        logger.exception("Failed to update DynamoDB item")

    formatted_state_name = replace_special_characters(state_name)
    parameter_name = f"/{state_machine_name}/{formatted_state_name}"
    logger.info(
        "Setting SSM parameter '%s' to value '%s'", parameter_name, value
    )
    client.put_parameter(
        Name=parameter_name, Overwrite=True, Type="String", Value=value
    )


def lambda_handler(event, context):
    logger.info("Lambda triggered with event '%s'", event)

    region = os.environ["AWS_REGION"]
    metric_namespace = os.environ["METRIC_NAMESPACE"]
    metric_dimension = os.environ["METRIC_DIMENSION"]
    state_names = json.loads(os.environ["STATE_NAMES"])
    logger.info("Collecting metrics for states '%s'", state_names)

    status = event["detail"]["status"]
    state_machine_arn = event["detail"]["stateMachineArn"]
    execution_arn = event["detail"]["executionArn"]
    timestamp = event["time"]
    state_machine_name = state_machine_arn.split(":")[6]
    execution_name = execution_arn.split(":")[7]

    if not len(
        set([replace_special_characters(s) for s in state_names])
    ) == len(state_names):
        raise ValueError(
            "Distinct state names map to the same name when replacing special characters with underscore"
        )

    client = boto3.client("stepfunctions")
    response = client.get_execution_history(
        executionArn=execution_arn, maxResults=500, reverseOrder=True
    )
    events = response["events"]

    detailed_states = [
        get_state_info(state_name, state_machine_name, events)
        for state_name in state_names
    ]
    logger.info(
        "Using detailed information about the states '%s'", detailed_states
    )

    metrics = []

    """
    Collect metric on individidual states and update SSM if necessary
    """
    for state in detailed_states:
        if not state["has_entered_state"]:
            logger.info(
                "Not collecting metrics for state '%s' as it was never entered during execution",
                state["state_name"],
            )
            continue
        # TODO Report timestamp of the event in question
        dimensions = [
            {"Name": "PipelineName", "Value": state_machine_name},
            {"Name": "StateName", "Value": state["state_name"]},
        ]
        metric_name = "StateSuccess"
        if state["has_successfully_exited_state"]:
            logger.info(
                "State '%s' was entered and successfully exited",
                state["state_name"],
            )
        else:
            logger.info(
                "State '%s' was entered, but did not successfully exit",
                state["state_name"],
            )
            if state["ssm_value"] and state["ssm_value"]["fixed"]:
                new_ssm_value = json.dumps(
                    {
                        "failed_execution": execution_arn,
                        "failed_at": event["detail"]["stopDate"],
                        "fixed": False,
                        "fixed_execution": None,
                        "fixed_at": None,
                    },
                )
                # TODO: Check if parameter already has been updated after the current execution's end time
                # ssm_last_modified = p["Parameter"]["LastModifiedDate"]
                set_ssm_value_for_state(
                    new_ssm_value, state["state_name"], state_machine_name
                )

            metric_name = "StateFail"
            dimensions.append(
                {
                    "Name": "FailType",
                    "Value": "TERRAFORM_LOCK"
                    if state["fail_event"]
                    and "Terraform acquires a state lock"
                    in state["fail_event"]["failedEventDetails"]["cause"]
                    else "DEFAULT",
                }
            )
        metrics.append(
            {
                "MetricName": metric_name,
                "Dimensions": dimensions,
                "Timestamp": timestamp,
                "Value": 1,
                "Unit": "Count",
            }
        )

        if (
            state["has_successfully_exited_state"]
            and state["ssm_value"]
            and not state["ssm_value"]["fixed"]
        ):
            logger.info(
                "State '%s' has been restored from a failure in an earlier execution '%s'",
                state["state_name"],
                state["ssm_value"]["failed_execution"],
            )
            new_ssm_value = json.dumps(
                {
                    **state["ssm_value"],
                    "fixed": True,
                    "fixed_execution": execution_arn,
                    "fixed_at": int(
                        datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S%z")
                        * 1000
                    ),
                },
            )
            set_ssm_value_for_state(
                new_ssm_value, state["state_name"], state_machine_name
            )

            # Do not update MeanTimeToRecovery if previously failed state failed because of Terraform lock
            failed_execution = get_detailed_execution(
                {"executionArn": state["ssm_value"]["failed_execution"]}
            )
            failed_execution_event = get_fail_event_for_state(
                state["state_name"], failed_execution["events"]
            )
            if (
                failed_execution_event
                and "Terraform acquires a state lock"
                in failed_execution_event["failedEventDetails"]["cause"]
            ):
                logger.info(
                    "Not updating MeanTimeToRecovery for state '%s' as earlier execution failed due to Terraform lock",
                    state["state_name"],
                )
            else:
                metrics.append(
                    {
                        "MetricName": "MeanTimeToRecovery",
                        "Dimensions": [
                            {
                                "Name": "PipelineName",
                                "Value": state_machine_name,
                            },
                            {
                                "Name": "StateName",
                                "Value": state["state_name"],
                            },
                        ],
                        "Timestamp": timestamp,
                        "Value": event["detail"]["stopDate"]
                        - state["ssm_value"]["failed_at"],
                        "Unit": "Milliseconds",
                    }
                )

    if status == "SUCCEEDED":
        """
        Update lead time metric if execution was successful
        """
        start_time = event["detail"]["startDate"]
        end_time = event["detail"]["stopDate"]
        if (
            start_time
            and end_time
            and all(state["has_entered_state"] for state in detailed_states)
        ):
            duration = end_time - start_time
            metrics.append(
                {
                    "MetricName": "LeadTime",
                    "Dimensions": [
                        {"Name": "PipelineName", "Value": state_machine_name},
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
