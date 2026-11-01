# Terraform / OpenTofu Security & FinOps Rubric

1. **No Unauthenticated Public Ingress:** No security group (`aws_security_group`, `azurerm_network_security_group`, `google_compute_firewall`) may open SSH (`22`), RDP (`3389`), or database ports (`3306`, `5432`, `6379`, `27017`) to `0.0.0.0/0` or `::/0`.
2. **Least-Privilege IAM Policies:** No IAM policy document may grant `Action: "*"` with `Resource: "*"` without an explicit scoped condition.
3. **Encryption at Rest Enabled:** All stateful storage and database resources (EBS volumes, S3 buckets, RDS instances, EKS secrets) must have encryption at rest enabled.
4. **Mandatory FinOps Cost Allocation Tags:** All billable cloud resources must include `Environment`, `Owner` (or `Team`), and `Service` tags for FinOps cost attribution.
5. **FinOps Cost & Sizing Review:** Flag any oversized instance classes, multi-AZ NAT gateway duplications in non-production environments, or unattached storage definitions, and print a Markdown summary table with remediation diffs.
