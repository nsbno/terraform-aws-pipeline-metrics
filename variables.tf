variable "name_prefix" {
  description = "A prefix used for naming resources."
  type        = string
}

variable "state_machine_arns" {
  description = "ARNs of state machines to report metrics on."
  type        = list(string)
}

variable "states_to_display" {
  description = "Names of states to display in the CloudWatch Dashboard."
  type        = list(string)
}

variable "tags" {
  description = "A map of tags (key-value pairs) passed to resources."
  type        = map(string)
  default     = {}
}

variable "lambda_timeout" {
  description = "The maximum number of seconds the Lambda is allowed to run"
  default     = 300
}

variable "schedule_expression" {
  description = "The schedule expression (in UTC) to use for the CloudWatch Event Rule that triggers the Lambda."
  default     = "rate(1 hour)"
}


