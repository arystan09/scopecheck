# ADR 0002: PostgreSQL queue and conservative budgets

Status: records the implemented baseline.

Short SKIP LOCKED claims, database-time leases and generation fencing authorize
writes/reservations. Execute outside transactions; reuse finished cases and reset
incomplete attempts. At most two cases/run, one run/worker, two attempts/case.
Transactional reservations serialize the final budget race. Unknown remote outcomes
retain their reservation; known usage settles it. This sacrifices throughput and
may exhaust budget conservatively. In-flight provider requests can outlive a lease:
no exactly-once remote execution claim. PostgreSQL suits this bounded low-volume
queue; it is not a claim of unlimited worker scalability. Recovery tests remain
mandatory regardless of coverage percentage.
