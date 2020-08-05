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
from datetime import datetime, date, timedelta, timezone

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def serialize_date(obj):
    if isinstance(obj, (date, datetime)):
        return {"__date__": True, "value": obj.isoformat()}
    return str(obj)


def deserialize_date(dct):
    if "__date__" in dct:
        return datetime.fromisoformat(dct["value"])
    return dct


def find_event_by_backtracking(initial_event, events, condition_fn):
    """Backtracks to the first event that matches a specific condition and returns that event"""
    event = initial_event
    visited_events = []
    for _ in range(len(events)):
        if condition_fn(event, visited_events):
            return event
        visited_events.append(event)
        event = next(
            (e for e in events if e["id"] == event["previousEventId"]), None
        )
        if event is None:
            break
    return event


def get_enter_event(state, events):
    """Check if a given state has been entered at some point during an execution"""
    return next(
        (
            e
            for e in events
            if e["type"].endswith("StateEntered")
            and e["stateEnteredEventDetails"]["name"] == state
        ),
        None,
    )


def get_names_of_entered_states(events, only_task_states=True):
    """Return the names of all states entered during an execution, optionally only including states of type `Task`"""
    state_names = set(
        [
            e["stateEnteredEventDetails"]["name"]
            for e in events
            if e.get("stateEnteredEventDetails", False)
            and (
                not only_task_states
                or (only_task_states and e["type"] == "TaskStateEntered")
            )
        ]
    )
    return state_names


def get_fail_event(state, events):
    """Return the event that made a given state fail during an execution"""
    fail_event = next(
        (
            e
            for e in events
            if e["type"].endswith("Failed")
            and any(
                key
                for key in e
                if key.endswith("FailedEventDetails")
                and all(
                    required_key in e[key]
                    for required_key in ["error", "cause"]
                )
            )
            and find_event_by_backtracking(
                e,
                events,
                lambda e2, visited_events: e2["type"].endswith("StateEntered")
                and e2["stateEnteredEventDetails"]["name"] == state
                and not any(
                    visited_event["type"].endswith("StateEntered")
                    for visited_event in visited_events
                ),
            )
        ),
        None,
    )
    if fail_event:
        # Different state types use different names for storing details about the failed event
        # taskFailedEventDetails, activityFailedEventDetails, etc.
        logger.info("State '%s failed in event '%s'", state, fail_event)
        fail_event_details_key = next(
            (
                key
                for key in fail_event
                if key.endswith("FailedEventDetails")
                and all(
                    required_key in fail_event[key]
                    for required_key in ["error", "cause"]
                )
            ),
            None,
        )
        return {
            **fail_event,
            "failedEventDetails": fail_event.get(fail_event_details_key),
        }
    return None


def get_success_event(state, events):
    """Check if a given state has successfully exited at some point during an execution"""
    exit_event = get_exit_event(state, events)
    if exit_event:
        success_event = find_event_by_backtracking(
            exit_event,
            events,
            lambda e, _: e["id"] == exit_event["previousEventId"]
            and e["type"].endswith("Succeeded"),
        )
        return success_event
    return None


def get_exit_event(state, events):
    """Check if a given state has been exited at some point during an execution"""
    return next(
        (
            e
            for e in events
            if e["type"].endswith("StateExited")
            and e["stateExitedEventDetails"]["name"] == state
        ),
        None,
    )


def get_detailed_execution(execution, limit=500, client=None):
    """Augment an execution with its execution history

    Args:
        execution: A dictionary containing at least the key `executionArn`
        limit: Limit the number of events in the execution history
        client: A Step Functions client

    Returns:
        The input execution augmented with its execution history

    Raises:
        botocore.exceptions.ClientError: Failed to get execution history
    """
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


def get_detailed_executions(
    state_machine_arn, limit=100, client=None,
):
    """Returns a list of detailed executions"""
    if client is None:
        client = boto3.client("stepfunctions")
    start = timer()
    executions = client.list_executions(
        stateMachineArn=state_machine_arn, maxResults=limit,
    )["executions"]
    end = timer()
    logger.info("Took %s s to list %s executions", end - start, limit)
    results = []
    start = timer()
    with PoolExecutor(max_workers=4) as executor:
        for res in executor.map(
            lambda e: get_detailed_execution(e, client=client), executions
        ):
            results.append(res)
    end = timer()
    logger.info(
        "Took %s s to get execution history of %s executions in parallel",
        end - start,
        limit,
    )
    return results


def save_execution_data_to_s3(executions, s3_bucket, s3_key):
    """Save Step Function execution data to S3"""
    s3 = boto3.resource("s3")
    try:
        obj = s3.Object(s3_bucket, s3_key)
    except botocore.exceptions.ClientError:
        logger.exception(
            "Something went wrong when trying to download file 's3://%s/%s'",
            s3_bucket,
            s3_key,
        )
        raise

    body = json.dumps(executions, default=serialize_date)
    obj.put(Body=body)


def get_execution_data_from_s3(s3_bucket, s3_key):
    """Return Step Function execution data saved in S3"""
    s3 = boto3.resource("s3")
    try:
        obj = s3.Object(s3_bucket, s3_key)
        obj.load()  # Will fail if file does not exist
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Code"] == "404":
            logger.debug("File 's3://%s/%s' does not exist", s3_bucket, s3_key)
            return []
        else:
            logger.exception(
                "Something went wrong when trying to download file 's3://%s/%s'",
                s3_bucket,
                s3_key,
            )
            raise
    body = obj.get()["Body"].read().decode("utf-8")
    try:
        saved_executions = json.loads(body, object_hook=deserialize_date)
    except (TypeError, json.decoder.JSONDecodeError):
        logger.exception(
            "Something went wrong when trying to load file content '%s' as JSON",
            body,
        )
        raise
    return saved_executions


def get_state_events(state_name, events):
    """Return a dictionary of various events for a given state"""
    return {
        "fail_event": get_fail_event(state_name, events),
        "success_event": get_success_event(state_name, events),
        "exit_event": get_exit_event(state_name, events),
        "enter_event": get_enter_event(state_name, events),
    }


def get_state_info(state_name, state_machine_name, events, table):
    """Return a dictionary containing various data for a given state in a given execution"""
    state_data = get_state_data_from_dynamodb(
        state_name, state_machine_name, table
    )
    state_events = get_state_events(state_name, events)

    return {"state_name": state_name, "state_data": state_data, **state_events}


def get_state_data_from_dynamodb(state_name, state_machine_name, table):
    """Fetch data about a given state from DynamoDB

    Args:
        state_machine_name: The name of the state machine
        state_name: The name of the state
        table: A DynamoDB.Table instance

    Returns:
        An item stored in the given DynamoDB table under the compound key made up of `state_machine_name` and `state_name` if such an item exists

    Raises:
        botocore.exceptions.ClientError: Failed to get item from DynamoDB
    """
    try:
        response = table.get_item(
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
                k: v if not isinstance(v, decimal.Decimal) else int(v)
                for k, v in item.items()
            }
            logger.info("Found DynamoDB item '%s'", item)
            return item
        else:
            logger.info(
                "Did not find DynamoDB item, got response '%s'", response
            )
            return None
    except botocore.exceptions.ClientError as e:
        logger.exception(
            "Failed to get DynamoDB item, API responded with '%s'", e.response,
        )
        raise


def set_state_data_in_dynamodb(state_data, table):
    """Save data about a state to DynamoDB

    Args:
        state_data: The item to save to the DynamoDB table
        table: A DynamoDB.Table instance

    Raises:
        botocore.exceptions.ClientError: Failed to create or update the item in the given DynamoDB table
        ValueError: State data is missing one or more of the keys that make up the composite primary key in the DynamoDB table
    """
    required_keys = ["state_machine_name", "state_name"]
    if not all(key in state_data for key in required_keys):
        raise ValueError(
            "State data missing one or more required keys for the DynamoDB composite primary key"
        )
    try:
        logger.info("Updating DynamoDB Item '%s'", state_data)
        item = table.put_item(Item=state_data)
    except botocore.exceptions.ClientError as e:
        logger.exception(
            "Failed to update DynamoDB item, API responded with '%s'",
            e.response,
        )
        raise


def get_metrics(state_machine_name, executions):
    # How to know which metrics have already been published?
    # Can use new_executions?
    # Only add executions that have succeeded, failed, cancelled or timed out (i.e., finished executions)
    # Running executions are ignored
    metrics = []
    failed_states = {}
    for e in executions:
        detailed_states = [
            {
                "state_name": state_name,
                **get_state_events(state_name, e["events"]),
            }
            for state_name in get_names_of_entered_states(e["events"])
        ]
        metrics.append(
            {
                "MetricName": "PipelineSuccess"
                if e["status"] == "SUCCEEDED"
                else "PipelineFail",
                "Timestamp": e["stopDate"],
                "Dimensions": [
                    {"Name": "PipelineName", "Value": state_machine_name},
                ],
                "Value": int(
                    e["stopDate"].timestamp() * 1000
                    - e["startDate"].timestamp() * 1000
                ),
                "Unit": "Milliseconds",
            }
        )
        for state in detailed_states:
            # TODO: Is success_event without exit_event even possible?
            state_name = state["state_name"]
            if state["success_event"]:
                metrics.append(
                    {
                        "MetricName": "StateSuccess",
                        "Timestamp": state["success_event"]["timestamp"],
                        "Dimensions": [
                            {
                                "Name": "PipelineName",
                                "Value": state_machine_name,
                            },
                            {"Name": "StateName", "Value": state_name,},
                        ],
                        "Value": int(
                            state["success_event"]["timestamp"].timestamp()
                            * 1000
                            - state["enter_event"]["timestamp"].timestamp()
                            * 1000
                        ),
                        "Unit": "Milliseconds",
                    }
                )
                # Check if recovered from failed state, in which case calculate MTTR
                if (
                    failed_states.get(state_name, None)
                    and failed_states[state_name]["startDate"].timestamp()
                    < e["startDate"].timestamp()
                ):
                    metrics.append(
                        {
                            "MetricName": "StateRecovery",
                            "Dimensions": [
                                {
                                    "Name": "PipelineName",
                                    "Value": state_machine_name,
                                },
                                {"Name": "StateName", "Value": state_name,},
                            ],
                            "Timestamp": state["success_event"]["timestamp"],
                            "Value": int(
                                state["success_event"]["timestamp"].timestamp()
                                * 1000
                                - failed_states[state_name]["fail_event"][
                                    "timestamp"
                                ].timestamp()
                                * 1000
                            ),
                            "Unit": "Milliseconds",
                        }
                    )
                    failed_states[state_name] = None

            if state["fail_event"]:
                if (
                    not failed_states.get(state_name, None)
                    or state["fail_event"]["timestamp"].timestamp()
                    < failed_states[state_name]["fail_event"][
                        "timestamp"
                    ].timestamp()
                ):
                    failed_states[state_name] = {**e, **state}
                metrics.append(
                    {
                        "MetricName": "StateFail",
                        "Timestamp": state["fail_event"]["timestamp"],
                        "Dimensions": [
                            {
                                "Name": "PipelineName",
                                "Value": state_machine_name,
                            },
                            {"Name": "StateName", "Value": state_name},
                            {
                                "Name": "FailType",
                                "Value": "TERRAFORM_LOCK"
                                if "Terraform acquires a state lock"
                                in state["fail_event"]["failedEventDetails"][
                                    "cause"
                                ]
                                else "DEFAULT",
                            },
                        ],
                        "Value": int(
                            state["fail_event"]["timestamp"].timestamp() * 1000
                            - state["enter_event"]["timestamp"].timestamp()
                            * 1000
                        ),
                        "Unit": "Milliseconds",
                    }
                )

    return metrics


def lambda_handler(event, context):
    logger.info("Lambda triggered with event '%s'", event)

    region = os.environ["AWS_REGION"]
    metric_namespace = os.environ["METRIC_NAMESPACE"]
    s3_bucket = os.environ["S3_BUCKET"]
    state_names = json.loads(os.environ["STATE_NAMES"])
    state_machine_arns = json.loads(os.environ["STATE_MACHINE_ARNS"])
    today = datetime.now(timezone.utc)
    for state_machine_arn in state_machine_arns:
        metrics = []
        state_machine_name = state_machine_arn.split(":")[6]
        s3_key = f"{state_machine_name}/executions.json"

        detailed_executions = get_detailed_executions(
            state_machine_arn, limit=100
        )
        detailed_executions = sorted(
            detailed_executions, key=lambda e: e["startDate"]
        )
        completed_executions = []
        for e in detailed_executions:
            if e["status"] == "RUNNING":
                break
            completed_executions.append(e)
        saved_executions = get_execution_data_from_s3(s3_bucket, s3_key)
        names_of_saved_executions = list(
            map(lambda e: e["name"], saved_executions)
        )
        new_executions = (
            list(
                filter(
                    lambda e: e["name"] not in names_of_saved_executions,
                    completed_executions,
                )
            )
            if len(saved_executions)
            else completed_executions
        )

        metrics = metrics + get_metrics(state_machine_name, new_executions)
        logger.info(
            "Found %s new, completed executions and %s metrics for state machine '%s'",
            len(new_executions),
            len(metrics),
            state_machine_name,
        )
        filtered_metrics = list(
            filter(
                lambda m: m["Timestamp"] > (today - timedelta(weeks=2)),
                metrics,
            )
        )
        if len(metrics) != len(filtered_metrics):
            logger.info(
                "Removed %s metrics as they were more than two weeks old",
                len(metrics) - len(filtered_metrics),
            )

        batch_size = 20
        cloudwatch = boto3.client("cloudwatch")
        for i in range(0, len(filtered_metrics), batch_size):
            batch = filtered_metrics[i : i + batch_size]
            response = cloudwatch.put_metric_data(
                Namespace=metric_namespace, MetricData=batch,
            )
        all_executions = sorted(
            saved_executions + new_executions, key=lambda e: e["startDate"]
        )
        save_execution_data_to_s3(all_executions, s3_bucket, s3_key)

    return

    status = event["detail"]["status"]
    state_machine_arn = event["detail"]["stateMachineArn"]
    execution_arn = event["detail"]["executionArn"]
    timestamp = event["time"]
    state_machine_name = state_machine_arn.split(":")[6]
    detailed_executions = get_detailed_executions(state_machine_arn)
    save_execution_data_to_s3(
        detailed_executions, s3_bucket, f"{state_machine_name}/executions.json"
    )

    dynamodb = boto3.resource("dynamodb")
    dynamodb_table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])

    execution_name = execution_arn.split(":")[7]

    client = boto3.client("stepfunctions")
    response = client.get_execution_history(
        executionArn=execution_arn, maxResults=500, reverseOrder=True
    )
    events = response["events"]

    if len(state_names) == 0:
        state_names = get_names_of_entered_states(events)

    logger.info("Collecting metrics for states '%s'", state_names)

    detailed_states = [
        get_state_info(state_name, state_machine_name, events, dynamodb_table)
        for state_name in state_names
    ]
    logger.info(
        "Using detailed information about the states '%s'", detailed_states
    )

    metrics = []

    for state in detailed_states:
        if not state["enter_event"]:
            logger.info(
                "Not collecting metrics for state '%s' as it was never entered during execution",
                state["state_name"],
            )
            continue
        dimensions = [
            {"Name": "PipelineName", "Value": state_machine_name},
            {"Name": "StateName", "Value": state["state_name"]},
        ]
        if state["success_event"] and state["exit_event"]:
            logger.info(
                "State '%s' was entered and successfully exited",
                state["state_name"],
            )
            metrics.append(
                {
                    "MetricName": "StateSuccess",
                    "Dimensions": dimensions,
                    "Timestamp": state["exit_event"]["timestamp"],
                    "Value": int(
                        state["exit_event"]["timestamp"].timestamp() * 1000
                        - state["enter_event"]["timestamp"].timestamp() * 1000
                    ),
                    "Unit": "Milliseconds",
                }
            )
        elif state["fail_event"]:
            logger.info(
                "State '%s' was entered, but did not successfully exit",
                state["state_name"],
            )
            if state["state_data"] and state["state_data"]["fixed"]:
                new_state_data = {
                    **state["state_data"],
                    "failed_execution": execution_arn,
                    "failed_at": int(
                        (
                            state["exit_event"]["timestamp"].timestamp()
                            if state["exit_event"]
                            else state["fail_event"]["timestamp"].timestamp()
                        )
                        * 1000
                    ),
                    "fixed": False,
                    "fixed_execution": None,
                    "fixed_at": None,
                }
                # TODO: Check if state data already has been updated after the current execution's end time
                set_state_data_in_dynamodb(new_state_data, dynamodb_table)
            elif not state["state_data"]:
                # Initial state data
                state_data = {
                    "state_machine_name": state_machine_name,
                    "state_name": state["state_name"],
                    "failed_execution": execution_arn,
                    "failed_at": int(
                        (
                            state["exit_event"]["timestamp"].timestamp()
                            if state["exit_event"]
                            else state["fail_event"]["timestamp"].timestamp()
                        )
                        * 1000
                    ),
                    "fixed": False,
                    "fixed_execution": None,
                    "fixed_at": None,
                }
                set_state_data_in_dynamodb(state_data, dynamodb_table)

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
            # exit_event may be undefined if the state failed the execution (e.g., `Raise Errors``)
            metrics.append(
                {
                    "MetricName": "StateFail",
                    "Dimensions": dimensions,
                    "Timestamp": state["exit_event"]["timestamp"]
                    if state.get("exit_event")
                    else state["fail_event"]["timestamp"],
                    "Value": int(
                        (
                            state["exit_event"]["timestamp"].timestamp()
                            if state.get("exit_event")
                            else state["fail_event"]["timestamp"].timestamp()
                        )
                        * 1000
                        - state["enter_event"]["timestamp"].timestamp() * 1000
                    ),
                    "Unit": "Milliseconds",
                }
            )

        else:
            logger.warn(
                "State '%s' did not contain success AND exit event, or fail event",
                state["state_name"],
            )
            continue

        if (
            state["success_event"]
            and state["exit_event"]
            and state["state_data"]
            and not state["state_data"]["fixed"]
        ):
            if state["state_data"]["failed_at"] < event["detail"]["startDate"]:
                logger.info(
                    "State '%s' has been restored from a failure in an earlier execution '%s'",
                    state["state_name"],
                    state["state_data"]["failed_execution"],
                )
                new_state_data = {
                    **state["state_data"],
                    "fixed": True,
                    "fixed_execution": execution_arn,
                    "fixed_at": int(
                        state["exit_event"]["timestamp"].timestamp() * 1000
                    ),
                }
                set_state_data_in_dynamodb(new_state_data, dynamodb_table)

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
                        "Timestamp": state["exit_event"]["timestamp"],
                        "Value": int(
                            state["exit_event"]["timestamp"].timestamp() * 1000
                        )
                        - state["state_data"]["failed_at"],
                        "Unit": "Milliseconds",
                    }
                )
            else:
                logger.info(
                    "State '%s' was in a failed state, but the failure occured AFTER the current execution had started, so skipping metric collecting for MeanTimeToRecovery",
                    state["state_name"],
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
            and all(state["enter_event"] for state in detailed_states)
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
