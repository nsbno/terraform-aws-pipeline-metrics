#!/usr/bin/env python
#
# Copyright (C) 2020 Erlend Ekern <dev@ekern.me>
#
# Distributed under terms of the MIT license.

"""

"""
from concurrent.futures import ThreadPoolExecutor as PoolExecutor
from timeit import default_timer as timer
from functools import reduce
import decimal
import os
import logging
import json
import urllib
import boto3
import botocore
from datetime import datetime

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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


def get_fail_event(state, events):
    """Return the event that made a given state fail during an execution"""
    fail_event = next(
        (
            e
            for e in events
            if e["type"].endswith("Failed")
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


def get_state_events(state_name, events):
    """Return a dictionary of various events for a given state"""
    return {
        "fail_event": get_fail_event(state_name, events),
        "success_event": get_success_event(state_name, events),
        "exit_event": get_exit_event(state_name, events),
        "enter_event": get_enter_event(state_name, events),
    }


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


def metrics():
    """[WIP] Create plots using matplotlib and API. Can be used to periodically create plots instead of running every time an execution succeeds or fails."""
    import matplotlib.pyplot as plt
    import numpy as np

    states = [
        "Deploy Test",
        "Deploy Stage",
        "Deploy Prod",
        "Integration Tests",
    ]
    executions = get_detailed_executions(
        "arn:aws:states:eu-west-1:929368261477:stateMachine:trafficinfo-state-machine",
        limit=500,
    )
    executions = list(filter(lambda e: e["status"] != "RUNNING", executions))
    executions = list(
        map(
            lambda e: {
                **e,
                "states": {
                    state: get_state_events(state, e["events"])
                    for state in states
                },
            },
            executions,
        )
    )
    executions_grouped_by_day = reduce(
        lambda acc, curr: {
            **acc,
            curr["startDate"].strftime("%Y-%m-%d"): acc.get(
                curr["startDate"].strftime("%Y-%m-%d"), []
            )
            + [curr],
        },
        executions,
        {},
    )
    total = {}
    for day, executions in executions_grouped_by_day.items():
        metrics = get_metrics(executions)
        total[day] = metrics

    metrics_grouped_by_state = {}
    for day, metrics in total.items():
        for state, metric in metrics.items():
            state_dict = metrics_grouped_by_state.get(state, {})
            for metric_name, metric_value in metric.items():
                v = state_dict.get(
                    metric_name, {"timeseries": [], "values": []}
                )
                v["timeseries"] = v["timeseries"] + [day]
                v["values"] = v["values"] + [metric_value]
                state_dict[metric_name] = v
                metrics_grouped_by_state[state] = state_dict
    print(total)
    print(metrics_grouped_by_state)
    # plt.xkcd()
    with plt.style.context("ggplot"):
        for state, metrics in metrics_grouped_by_state.items():
            fig, ax = plt.subplots(2, 2)
            fig.suptitle(state)
            ax[0, 0].set_title(f"Deployment Frequency")
            ax[0, 0].set_xlabel("Day")
            ax[0, 0].set_ylabel("Successes")
            # ax.set_prop_cycle(cycler("linestyle", ["-", "--", ":", "-."]))
            # ax.set_xticks(rotation=45)
            # ax[0, 0].set_xticks(ax[0, 0].get_xticks())[::5]
            ax[0, 0].plot(
                list(reversed(metrics["success"]["timeseries"])),
                list(reversed(metrics["success"]["values"])),
                label=state,
            )
            ax[0, 1].set_title(f"Mean Time to Recovery")
            ax[0, 1].set_xlabel("Day")
            ax[0, 1].set_ylabel("Minutes")
            # ax.s1t_prop_cycle(cycler("linestyle", ["-", "--", ":", "-."]))
            # ax.1et_xticks(rotation=45)
            # ax[0, 1].set_xticks(ax[0, 0].get_xticks())[::5]
            ax[0, 1].plot(
                list(reversed(metrics["mttr"]["timeseries"])),
                np.array(list(reversed(metrics["mttr"]["values"]))) / 60,
                label=state,
            )
            fig.tight_layout()

            # plt.xkcd()
            # plt.title(f"{state} - Mean Time to Recovery")
            # plt.xlabel("Day")
            # plt.ylabel("Seconds")
            # plt.xticks(rotation=45)
            # plt.plot(
            #    list(reversed(metrics["mttr"]["timeseries"])),
            #    list(reversed(metrics["mttr"]["values"])),
            # )
            # plt.show()
    plt.show()


def get_metrics(executions):
    """[WIP] Return the metrics for a set of executions"""
    metrics = {}
    failed_state = {}
    for e in sorted(executions, key=lambda e: e["startDate"]):
        for state_name, state in e["states"].items():
            state_metrics = metrics.get(state_name, {})
            if state["success_event"]:
                if e["startDate"].weekday() < 5:
                    state_metrics["success_weekday"] = (
                        state_metrics.get("success_weekday", 0) + 1
                    )
                state_metrics["success"] = state_metrics.get("success", 0) + 1
                if (
                    failed_state.get(state_name, None)
                    and failed_state[state_name]["startDate"] < e["startDate"]
                ):
                    duration = int(
                        state["success_event"]["timestamp"].timestamp()
                    ) - int(
                        failed_state[state_name]["fail_event"][
                            "timestamp"
                        ].timestamp()
                    )
                    state_metrics["recovery"] = (
                        state_metrics.get("recovery", 0) + 1
                    )
                    state_metrics["broken"] = (
                        state_metrics.get("broken", 0) + duration
                    )
                    state_metrics["mttr"] = state_metrics.get(
                        "broken"
                    ) / state_metrics.get("recovery")
                    print(
                        f"Failed state {state_name} in {failed_state[state_name]['executionArn'].split(':')[7]} fixed by {e['executionArn'].split(':')[7]}"
                    )
                    failed_state[state_name] = None
            if state["fail_event"]:
                if (
                    not failed_state.get(state_name, None)
                    or state["fail_event"]["timestamp"].timestamp()
                    < failed_state.get(state_name)["fail_event"][
                        "timestamp"
                    ].timestamp()
                ):
                    failed_state[state_name] = {**e, **state}
                state_metrics["fail"] = state_metrics.get("fail", 0) + 1
            if state["enter_event"]:
                state_metrics["enter"] = state_metrics.get("enter", 0) + 1
            metrics[state_name] = state_metrics

    return metrics


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


def lambda_handler(event, context):
    logger.info("Lambda triggered with event '%s'", event)

    region = os.environ["AWS_REGION"]
    metric_namespace = os.environ["METRIC_NAMESPACE"]
    state_names = json.loads(os.environ["STATE_NAMES"])

    logger.info("Collecting metrics for states '%s'", state_names)

    status = event["detail"]["status"]
    state_machine_arn = event["detail"]["stateMachineArn"]
    execution_arn = event["detail"]["executionArn"]
    timestamp = event["time"]
    state_machine_name = state_machine_arn.split(":")[6]

    dynamodb = boto3.resource("dynamodb")
    dynamodb_table = dynamodb.Table(os.environ["DYNAMODB_TABLE"])

    execution_name = execution_arn.split(":")[7]

    client = boto3.client("stepfunctions")
    response = client.get_execution_history(
        executionArn=execution_arn, maxResults=500, reverseOrder=True
    )
    events = response["events"]

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
            metric_name = "StateSuccess"
        else:
            logger.info(
                "State '%s' was entered, but did not successfully exit",
                state["state_name"],
            )
            if state["state_data"] and state["state_data"]["fixed"]:
                new_state_data = {
                    **state["state_data"],
                    "failed_execution": execution_arn,
                    "failed_at": int(
                        state["exit_event"]["timestamp"].timestamp() * 1000
                    ),
                    "fixed": False,
                    "fixed_execution": None,
                    "fixed_at": None,
                }
                # TODO: Check if state data already has been updated after the current execution's end time
                set_state_data_in_dynamodb(new_state_data, dynamodb_table)

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
                "Timestamp": state["exit_event"]["timestamp"],
                "Value": 1,
                "Unit": "Count",
            }
        )

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

                # Only update MeanTimeToRecovery if previously failed state failed because of Terraform lock
                failed_execution = get_detailed_execution(
                    {"executionArn": state["state_data"]["failed_execution"]}
                )
                failed_execution_event = get_fail_event(
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
                            "Timestamp": state["exit_event"]["timestamp"],
                            "Value": int(
                                state["exit_event"]["timestamp"].timestamp()
                                * 1000
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
