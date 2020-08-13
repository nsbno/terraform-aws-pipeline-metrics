locals {
  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets = concat(
      [{
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = "\nLead Time (LT) | Change Failure Rate (CFR) | Deployment Frequency (DF) | Mean Time to Recovery (MTTR) | Run Time (RT) \n----|-----|----|-----|-----\nPipeline execution time for executions that are successful | Percentage of times a given state has failed | Number of times a given state has been successful | Time it takes for a given state to go from failure to success | Time it takes for a given state to successfully complete\n"
        }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 24
          height = 3
          properties = {
            metrics = [
              [{ expression = "m2/(60*1000)", label = "(minutes) Lead Time", id = "e2" }],
              [local.metric_namespace, "StateMachineSuccess", "StateMachineName", each.key, { id = "m2", visible = false }],
            ]
            view   = "singleValue"
            region = local.current_region
            stat   = "Average"
            period = 86400
            title  = "Overall Pipeline Metrics"
          }
        }
      ],
      flatten([for state in var.states_to_display : [
        {
          type   = "text"
          x      = 0
          y      = 0
          width  = 24
          height = 2
          properties = {
            markdown = "&nbsp;\n# **${state}**"
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 24
          height = 3
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m3", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m2", stat = "Average", visible = false, label = "StateSuccessTime" }],
              [{ expression = "m2/(60*1000)", label = "(minutes) Run Time", id = "e4" }],
              [{ expression = "m3/(PERIOD(m3)/(3600*24))", id = "e2", label = "(#) Deployment Frequency" }],
              [{ expression = "100*(m4/(m3+m4))", id = "e1", label = "(%) Change Failure Rate" }],
              [{ expression = "m1/(60*1000)", label = "(minutes) Mean Time to Recovery", id = "e3" }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "DEFAULT", { label = "Other failures", id = "m4", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "TERRAFORM_LOCK", { label = "Terraform lock failures", id = "m5", stat = "SampleCount", visible = false }],
              [local.metric_namespace, "StateRecovery", "StateMachineName", each.key, "StateName", state, { id = "m1", label = "StateRecovery", visible = false }]
            ]
            view                 = "singleValue"
            region               = local.current_region
            stat                 = "Average"
            period               = 604800 # Set to large value to avoid incorrect values appearing when auto-refresh is enabled for the CloudWatch Dashboard
            title                = "Key Numbers (avg. daily)"
            setPeriodToTimeRange = true
          }
        },
        {
          type   = "metric"
          x      = 0
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [{ expression = "m4/(1000*60)", id = "e1", label = "Run Time" }],
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Average"
            period   = 86400
            title    = "Run Time"
            yAxis = {
              left = {
                showUnits = false
                min       = 0
                label     = "Minutes"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 6
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { stat = "SampleCount", id = "m3", label = "Deployment Frequency" }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Sum"
            period   = 86400
            title    = "Deployment Frequency"
            yAxis = {
              left = {
                min       = 0
                showUnits = false
                label     = "Frequency"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 12
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [local.metric_namespace, "StateSuccess", "StateMachineName", each.key, "StateName", state, { id = "m3", stat = "SampleCount", label = "(#) Deployment frequency", visible = false }],
              [{ expression = "100*(m4/(m3+m4))", id = "e1", label = "Change Failure Rate" }],
              [local.metric_namespace, "StateFail", "StateMachineName", each.key, "StateName", state, "FailType", "DEFAULT", { stat = "SampleCount", label = "Other failures", id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Sum"
            period   = 86400
            title    = "Change Failure Rate"
            yAxis = {
              left = {
                min       = 0
                max       = 100
                showUnits = false
                label     = "Percentage"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        },
        {
          type   = "metric"
          x      = 18
          y      = 0
          width  = 6
          height = 6
          properties = {
            metrics = [
              [{ expression = "m4/(1000*60)", id = "e1", label = "Mean Time to Recovery" }],
              [local.metric_namespace, "StateRecovery", "StateMachineName", each.key, "StateName", state, { id = "m4", visible = false }]
            ]
            view     = "timeSeries"
            stacked  = false
            region   = local.current_region
            liveData = true
            stat     = "Average"
            period   = 86400
            title    = "Mean Time to Recovery"
            yAxis = {
              left = {
                showUnits = false
                min       = 0
                label     = "Minutes"
              }
            }
            legend = {
              position = "hidden"
            }
          }
        }
        ]
      ])
    )
  })
}
