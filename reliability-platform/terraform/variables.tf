variable "region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "reliability-demo"
}
variable "admin_cidrs" {
  description = "CIDRs allowed to access the public Kubernetes API; set to your own public IP /32."
  type        = list(string)
  validation {
    condition     = length(var.admin_cidrs) > 0 && !contains(var.admin_cidrs, "0.0.0.0/0")
    error_message = "Provide restricted admin CIDRs, not 0.0.0.0/0."
  }
}
variable "kubernetes_version" {
  type    = string
  default = "1.34"
}
