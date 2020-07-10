variable "name_prefix" {
  description = "A prefix used for naming resources."
  type        = string
}

variable "state_machine_arns" {
  description = "ARNs of state machines to report metrics on."
  type        = list(string)
}

variable "states_to_collect" {
  description = "Names of states to collect metrics on. If this is an empty list, metrics will be collected for all states of type `Task`."
  default     = []
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
  default     = 60
}


