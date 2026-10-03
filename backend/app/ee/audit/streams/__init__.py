# Audit Log Streams
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# Deliver every audit event an organization produces to its SIEM or bucket
# (Datadog, Splunk, Microsoft Sentinel, S3, GCS, HTTPS, syslog) — at least
# once, resumable from the last delivered event. See
# docs/design/audit-log-streams.md.
