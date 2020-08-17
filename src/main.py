#!/usr/bin/env python
#
# Copyright (C) 2020 Erlend Ekern <dev@ekern.me>
#
# Distributed under terms of the MIT license.

"""
An AWS Lambda function for collecting and reporting metrics associated with
AWS Step Functions state machines.
"""
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from timeit import default_timer as timer
import os
import logging
import json
from datetime import datetime, date, timedelta, timezone
from functools import reduce

import boto3
import botocore

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def serialize_date(obj):
    """Serialize a datetime object to a string in ISO 8601 format"""
    if isinstance(obj, (date, datetime)):
        return {"__date__": True, "value": obj.isoformat()}
    return str(obj)


def deserialize_date(dct):
    """Deserialize a string in ISO 8601 format to a datetime object"""
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
        logger.debug("State '%s failed in event '%s'", state, fail_event)
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
    executions, client=None,
):
    """Return a list of detailed executions"""
    if client is None:
        client = boto3.client("stepfunctions")
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
        len(results),
    )
    return results


def get_state_events(state_name, events):
    """Return a dictionary of various relevant events for a given state"""
    return {
        "fail_event": get_fail_event(state_name, events),
        "success_event": get_success_event(state_name, events),
        "exit_event": get_exit_event(state_name, events),
        "enter_event": get_enter_event(state_name, events),
    }


def get_metrics(state_machine_name, executions):
    """Return metrics based on a list of detailed AWS Step Functions
    state machine executions. The data under each metric's `metric_data` key is
    in the format expected by CloudWatch when publishing metrics
    (i.e., `cloudwatch.put_metric_data(..., MetricData=metric["metric_data"]`).
    The other fields in each metric is used for constructing unique keys
    when saving data to DynamoDB.
    """
    logger.info(
        "Calculating metrics for %s executions in state machine '%s'",
        len(executions),
        state_machine_name,
    )
    metrics = []
    failed_execution = {}
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
                "execution": f'{e["executionArn"]}|{e["startDate"].isoformat()}',
                "metric": "StateMachineSuccess",
                "execution_arn": e["executionArn"],
                "start_date": e["startDate"].isoformat(),
                "metric_data": {
                    "MetricName": "StateMachineSuccess"
                    if e["status"] == "SUCCEEDED"
                    else "StateMachineFail",
                    "Timestamp": e["stopDate"],
                    "Dimensions": [
                        {
                            "Name": "StateMachineName",
                            "Value": state_machine_name,
                        },
                    ],
                    "Value": int(
                        (e["stopDate"] - e["startDate"]).total_seconds() * 1000
                    ),
                    "Unit": "Milliseconds",
                },
            }
        )
        if e["status"] == "SUCCEEDED" and failed_execution:
            metrics.append(
                {
                    "execution": f'{e["executionArn"]}|{e["startDate"].isoformat()}',
                    "metric": "StateMachineRecovery",
                    "execution_arn": e["executionArn"],
                    "start_date": e["startDate"].isoformat(),
                    "metric_data": {
                        "MetricName": "StateMachineRecovery",
                        "Timestamp": e["stopDate"],
                        "Dimensions": [
                            {
                                "Name": "StateMachineName",
                                "Value": state_machine_name,
                            },
                        ],
                        "Value": int(
                            (
                                e["stopDate"] - failed_execution["stopDate"]
                            ).total_seconds()
                            * 1000
                        ),
                        "Unit": "Milliseconds",
                    },
                }
            )
            failed_execution = {}
        elif e["status"] != "SUCCEEDED" and not failed_execution:
            failed_execution = e

        for state in detailed_states:
            # TODO: Is success_event without exit_event even possible?
            state_name = state["state_name"]
            if state["success_event"]:
                metrics.append(
                    {
                        "execution": f'{e["executionArn"]}|{e["startDate"].isoformat()}',
                        "metric": state_name + "|" + "StateSuccess",
                        "execution_arn": e["executionArn"],
                        "start_date": e["startDate"].isoformat(),
                        "metric_data": {
                            "MetricName": "StateSuccess",
                            "Timestamp": state["success_event"]["timestamp"],
                            "Dimensions": [
                                {
                                    "Name": "StateMachineName",
                                    "Value": state_machine_name,
                                },
                                {"Name": "StateName", "Value": state_name,},
                            ],
                            "Value": int(
                                (
                                    state["success_event"]["timestamp"]
                                    - state["enter_event"]["timestamp"]
                                ).total_seconds()
                                * 1000
                            ),
                            "Unit": "Milliseconds",
                        },
                    }
                )
                # Check if recovered from failed state, in which case calculate MTTR
                if (
                    failed_states.get(state_name, None)
                    and failed_states[state_name]["startDate"].timestamp()
                    < e["startDate"].timestamp()
                    and failed_states[state_name]["fail_event"]["timestamp"]
                    < state["success_event"]["timestamp"]
                ):
                    metrics.append(
                        {
                            "execution": f'{e["executionArn"]}|{e["startDate"].isoformat()}',
                            "metric": state_name + "|" + "StateRecovery",
                            "execution_arn": e["executionArn"],
                            "start_date": e["startDate"].isoformat(),
                            "metric_data": {
                                "MetricName": "StateRecovery",
                                "Dimensions": [
                                    {
                                        "Name": "StateMachineName",
                                        "Value": state_machine_name,
                                    },
                                    {
                                        "Name": "StateName",
                                        "Value": state_name,
                                    },
                                ],
                                "Timestamp": state["success_event"][
                                    "timestamp"
                                ],
                                "Value": int(
                                    (
                                        state["success_event"]["timestamp"]
                                        - failed_states[state_name][
                                            "fail_event"
                                        ]["timestamp"]
                                    ).total_seconds()
                                    * 1000
                                ),
                                "Unit": "Milliseconds",
                            },
                        }
                    )
                    failed_states[state_name] = None
            if state["fail_event"]:
                if (
                    not failed_states.get(state_name, None)
                    or state["fail_event"]["timestamp"]
                    < failed_states[state_name]["fail_event"]["timestamp"]
                ):
                    failed_states[state_name] = {**e, **state}
                metrics.append(
                    {
                        "execution": f'{e["executionArn"]}|{e["startDate"].isoformat()}',
                        "metric": state_name + "|" + "StateFail",
                        "execution_arn": e["executionArn"],
                        "start_date": e["startDate"].isoformat(),
                        "metric_data": {
                            "MetricName": "StateFail",
                            "Timestamp": state["fail_event"]["timestamp"],
                            "Dimensions": [
                                {
                                    "Name": "StateMachineName",
                                    "Value": state_machine_name,
                                },
                                {"Name": "StateName", "Value": state_name},
                                {
                                    "Name": "FailType",
                                    "Value": "TERRAFORM_LOCK"
                                    if "Terraform acquires a state lock"
                                    in state["fail_event"][
                                        "failedEventDetails"
                                    ]["cause"]
                                    else "DEFAULT",
                                },
                            ],
                            "Value": int(
                                (
                                    state["fail_event"]["timestamp"]
                                    - state["enter_event"]["timestamp"]
                                ).total_seconds()
                                * 1000
                            ),
                            "Unit": "Milliseconds",
                        },
                    }
                )

    return metrics


def get_deduplicated_metrics(metrics, dynamodb_table):
    """Return a list of metrics that does not already exist in DynamoDB"""
    deduplicated_metrics = []
    logger.info("Checking if any of the metrics already exist in DynamoDB")
    grouped_by_execution = reduce(
        lambda acc, curr: {
            **acc,
            curr["execution"]: acc.get(curr["execution"], []) + [curr],
        },
        metrics,
        {},
    )
    for execution, execution_metrics in grouped_by_execution.items():
        response = dynamodb_table.query(
            ConsistentRead=True,
            KeyConditionExpression=boto3.dynamodb.conditions.Key(
                "execution"
            ).eq(execution),
        )
        items = list(map(lambda item: item["metric"], response["Items"]))
        while response.get("LastEvaluatedKey", None):
            response = dynamodb_table.query(
                ExclusiveStartKey=response["LastEvaluatedKey"],
                ConsistentRead=True,
                KeyConditionExpression=boto3.dynamodb.conditions.Key(
                    "execution"
                ).eq(execution),
            )
            items += list(map(lambda item: item["metric"], response["Items"]))
        logger.debug(
            "Found %s items in DynamoDB with hash key '%s' %s",
            len(items),
            execution,
        )
        deduplicated_metrics += list(
            filter(
                lambda metric: metric["metric"] not in items,
                execution_metrics,
            )
        )

    logger.debug(
        "Found %s duplicate metrics", len(metrics) - len(deduplicated_metrics),
    )
    return deduplicated_metrics


def save_executions_to_s3(executions, s3_bucket, s3_prefix):
    """Save a list of executions to S3 -- one file per execution"""
    s3 = boto3.resource("s3")
    logger.info(
        "Saving %s newly processed executions in S3 bucket '%s' under prefix '%s'",
        len(executions),
        s3_bucket,
        s3_prefix,
    )
    for execution in executions:
        s3_key = f'{s3_prefix}/{int(execution["startDate"].timestamp() * 1000)}_{execution["name"]}.json'.lower()
        try:
            obj = s3.Object(s3_bucket, s3_key)
        except botocore.exceptions.ClientError:
            logger.exception(
                "Something went wrong when trying to load file 's3://%s/%s'",
                s3_bucket,
                s3_key,
            )
            raise

        body = json.dumps(execution, default=serialize_date)
        obj.put(Body=body)


def filter_processed_executions(executions, s3_bucket, s3_prefix):
    """Return a list of executions that have not had their execution data saved
    to S3 (i.e., unprocessed)"""
    s3 = boto3.client("s3")
    response = s3.list_objects_v2(Bucket=s3_bucket, Prefix=s3_prefix)
    try:
        objects = response["Contents"]
    except KeyError:
        logger.info(
            "Did not find any objects in bucket '%s' matching the prefix '%s'",
            s3_bucket,
            s3_prefix,
        )
        return executions

    while response["IsTruncated"]:
        response = s3.list_objects_v2(
            Bucket=s3_bucket,
            ContinuationToken=response["NextContinuationToken"],
            Prefix=s3_prefix,
        )
        objects = objects + response["Contents"]
    # Remove prefixes from all keys so we are left with just the filename
    filenames = list(
        map(
            lambda obj: obj["Key"][
                obj["Key"].startswith(f"{s3_prefix}/")
                and len(f"{s3_prefix}/") :
            ].lower(),
            objects,
        )
    )
    filenames = list(
        filter(lambda filename: filename.endswith(".json"), filenames)
    )
    unprocessed_executions = list(
        filter(
            lambda e: f'{int(e["startDate"].timestamp() * 1000)}_{e["name"]}.json'.lower()
            not in filenames,
            executions,
        )
    )
    return unprocessed_executions


def lambda_handler(event, context):
    logger.info("Lambda triggered with event '%s'", event)

    region = os.environ["AWS_REGION"]
    current_account_id = os.environ["CURRENT_ACCOUNT_ID"]
    dynamodb_table_name = os.environ["DYNAMODB_TABLE_NAME"]
    metric_namespace = os.environ["METRIC_NAMESPACE"]
    s3_bucket = os.environ["S3_BUCKET"]
    state_machine_arns = json.loads(os.environ["STATE_MACHINE_ARNS"])

    today = datetime.now(timezone.utc)
    sfn = boto3.client("stepfunctions")

    dynamodb = boto3.resource("dynamodb")
    dynamodb_table = dynamodb.Table(dynamodb_table_name)

    for state_machine_arn in state_machine_arns:
        state_machine_name = state_machine_arn.split(":")[6]
        s3_prefix = f"{current_account_id}/{state_machine_name}"
        executions = sfn.list_executions(
            stateMachineArn=state_machine_arn, maxResults=100,
        )["executions"]
        executions = sorted(executions, key=lambda e: e["startDate"])
        completed_executions = []
        for e in executions:
            if e["status"] == "RUNNING":
                break
            completed_executions.append(e)
        new_executions = filter_processed_executions(
            completed_executions, s3_bucket, s3_prefix
        )

        detailed_new_executions = get_detailed_executions(
            new_executions, client=sfn
        )

        metrics = get_metrics(state_machine_name, detailed_new_executions)
        logger.info(
            "Found %s unprocessed, completed executions and %s metrics for state machine '%s'",
            len(detailed_new_executions),
            len(metrics),
            state_machine_name,
        )
        filtered_metrics = list(
            filter(
                lambda m: m["metric_data"]["Timestamp"]
                > (today - timedelta(weeks=2)),
                metrics,
            )
        )
        if len(metrics) != len(filtered_metrics):
            logger.info(
                "Filtered out %s metrics as they were more than two weeks old",
                len(metrics) - len(filtered_metrics),
            )

        if len(filtered_metrics):
            deduplicated_metrics = get_deduplicated_metrics(
                filtered_metrics, dynamodb_table
            )
            logger.info(
                "%s metrics will be published to CloudWatch for state machine '%s'",
                len(deduplicated_metrics),
                state_machine_name,
            )

            # Batch the requests due to API limits (max. 20 metrics per API call)
            batch_size = 20
            cloudwatch = boto3.client("cloudwatch")
            for i in range(0, len(deduplicated_metrics), batch_size):
                batch_number = (i // batch_size) + 1
                retries = 0
                batch = deduplicated_metrics[i : i + batch_size]
                while True:
                    try:
                        response = cloudwatch.put_metric_data(
                            Namespace=metric_namespace,
                            MetricData=list(
                                map(lambda m: m["metric_data"], batch)
                            ),
                        )
                        break
                    except botocore.exceptions.ClientError:
                        logger.exception(
                            "Failed to publish batch #%s of metrics to CloudWatch",
                            batch_number,
                            state_machine_name,
                        )
                        if retries < 2:
                            retries += 1
                            continue
                        raise

                logger.debug(
                    "Successfully published batch #%s of metrics to CloudWatch",
                    batch_number,
                )

                retries = 0
                while True:
                    try:
                        with dynamodb_table.batch_writer() as batch_writer:
                            logger.debug(
                                "Saving batch #%s of metrics to DynamoDB",
                                batch_number,
                            )
                            for m in batch:
                                time_to_live = (
                                    m["metric_data"]["Timestamp"]
                                    + timedelta(days=15)
                                ).isoformat()
                                timestamp = m["metric_data"][
                                    "Timestamp"
                                ].isoformat()
                                item = {
                                    **m,
                                    "time_to_live": time_to_live,
                                    "metric_data": {
                                        **m["metric_data"],
                                        "Timestamp": timestamp,
                                    },
                                }
                                batch_writer.put_item(Item=item)
                        break
                    except botocore.exceptions.ClientError:
                        logger.exception(
                            "Failed to save batch #%s of metrics to DynamoDB",
                            batch_number,
                        )
                        if retries < 2:
                            retries += 1
                            continue

        if len(detailed_new_executions):
            save_executions_to_s3(
                detailed_new_executions, s3_bucket, s3_prefix
            )
        logger.info(
            "Finished metric collection and reporting for state machine '%s'",
            state_machine_name,
        )
