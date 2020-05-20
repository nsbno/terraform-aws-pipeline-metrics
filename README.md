## terraform-aws-pipeline-metrics

A Terraform module for reporting metrics wrt. a CD pipeline implemented in AWS Step Functions, and displaying these metrics in a CloudWatch dashboard.

A Lambda function is triggered every time a Step Function execution succeeds or fails, and metrics are sent to CloudWatch. For a given state machine, the following metrics are collected on a per-state basis:
- Deployment Frequency: number of times the state has successfully exited
- Change Failure Rate: percentage of times the state has failed versus succeeded, i.e., `100 * successes/(successes + failures)`.
- Mean Time to Recovery: time it takes for a state to go from failure to success

DynamoDB is used to store some intermediate data about the states that makes it easy to calculate the Mean Time to Recovery without having to use the AWS SDK to look up information about different executions and filter through them.
