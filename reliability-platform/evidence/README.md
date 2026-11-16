# Example local measurement

Measured 2026-11-16 in the Codex workspace with k6 0.57.0. PostgreSQL and Redis ran in Docker; the three application roles ran as local Python processes against them. This is a short functional demonstration, not a capacity benchmark or EKS result. The full Grafana/Prometheus stack could not be started in this environment because of container storage limits.

Both runs used 20 requests/s for 20 seconds, one inventory process with a 300 ms injected delay, and identical application settings except the inventory concurrency limit. k6 used the repository's `load/orders.js`. The degraded run had a limit of 4; the improved run had a limit of 16. Runs were sequential, with a service restart between them. The p95 is from k6's `http_req_duration` over all responses, and error rate is `http_req_failed`.

| Inventory concurrency | Requests | p95 latency | HTTP failure rate | Dropped iterations |
| --- | ---: | ---: | ---: | ---: |
| 4 | 401 | 3,009.78 ms | 73.07% | 0 |
| 16 | 400 | 319.55 ms | 0% | 0 |

The degraded run failed the scripted thresholds, as expected; the improved run passed. The 3-second orders-to-inventory timeout explains the cluster of long failures. The service change increases the number of concurrent delayed requests; it does not eliminate the injected 300 ms delay. Repeat on the target system, with longer runs, warm-up, resource measurements, confidence intervals, and multiple trials before drawing capacity conclusions. The two JSON files are k6 summary exports. `dropped_iterations` is absent when k6 did not emit any dropped-iteration samples.

A separate OTLP receiver check confirmed a shared trace ID across gateway, orders, inventory, and the asynchronous `process order` consumer span. That check did not validate the Jaeger UI or its storage.
