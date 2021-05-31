# terraform-aws-pipeline-metrics
A Terraform module for creating two Lambda functions:
- one that periodically calculates and reports granular metrics for one or multiple AWS Step Functions state machines
- a second that reports a few (near-)realtime metrics (e.g., for constructing CloudWatch Alarms if a given state fails).

## Metrics
The metrics are calculated using data from `boto3`'s `sfn` client, which among many other things, gives you access to the timestamped _events_ that occurred during a given execution (`TaskStateEntered`, `TaskSucceeded`, `TaskFailed`, etc.).

### Periodic metrics
Three metrics are calculated on an execution-basis, and these metrics are calculated using basic execution data (i.e., did the execution succeed? When did it start and end?):
- **StateMachineSuccess**: time it took for an execution to succeed (milliseconds).
- **StateMachineFail**: time it took for an execution to fail (milliseconds).
- **StateMachineRecovery**: time it took for the state machine to go from fail to success (milliseconds).

Three metrics are calculated for each state in an execution, and these metrics are calculated mainly using the different events for a given execution:
- **StateSuccess**: time it took for the state to succeed (milliseconds).
- **StateFail**: time it took for the state to fail (milliseconds).
- **StateRecovery**: time it took for the state to go from fail to success (milliseconds).

The "raw" data received from the API is further saved in S3 for future analysis, as data about a given execution is only available through the API for 90 days.

The metrics are published to CloudWatch as Custom Metrics and a CloudWatch dashboard is set up to visualize them. The metrics are additionally saved in DynamoDB to avoid publishing duplicate metrics in a later Lambda invocation.

### Realtime metrics
Under a separate namespace, two metrics are collected after each Step Functions state machine execution ends:
- **StateSuccess**: time it took for the state to succeed (milliseconds).
- **StateFail**: time it took for the state to fail (milliseconds).

## Caveats
Currently been tested on a large state machine without any loops, and mainly `Task` states.
