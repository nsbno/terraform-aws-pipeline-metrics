# terraform-aws-pipeline-metrics
A Terraform module for creating a Lambda that periodically calculates and reports granular metrics for one or multiple AWS Step Functions state machines.

## Metrics
The metrics are calculated using data from the `boto3`'s `sfn` client. The raw data received from the API is saved in S3 for future analysis, as data about a given execution is only available through the API for 90 days. Metrics are published to CloudWatch as Custom Metrics, and additionally saved in DynamoDB to avoid publishing duplicate metrics in a later Lambda run.

Two metrics are calculated for each execution:
- **StateMachineSuccess**: time it took for an execution to succeed (milliseconds).
- **StateMachineFail**: time it took for an execution to fail (milliseconds).
- **StateMachineRecovery**: time it took for the state machine to go from fail to success (milliseconds).

Three metrics can be calculated for each state in an execution:
- **StateSuccess**: time it took for the state to succeed (milliseconds)
- **StateFail**: time it took for the state to fail (milliseconds)
- **StateRecovery**: time it took for the state to go from fail to success (milliseconds)

## Caveats
Currently been tested on a fairly vanilla state machine without any loops, and mainly `Task` states.
