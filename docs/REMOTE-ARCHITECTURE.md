# Generic private worker architecture: design, not implementation

The existing primitives are `farm/queue.py`, `farm/worker.py`, known-target claiming,
atomic file claims, local build API, progress files, result/artifact publication,
and orchestrator ARM farm dispatch. Extend these, not a parallel generic shell API.
The current UI does not centrally schedule arbitrary OS workers.

## Required protocol

1. Administrator provisions a controller and individually revocable worker credential.
   The controller stores hashed credentials and explicit OS/architecture/target scopes.
   Worker credentials must not allow job submission, admin registration or other
   workers' uploads. A separate submitter credential cannot claim worker jobs.
2. Worker registers authenticated capabilities and supported immutable source profiles.
   Advertised capabilities are intersected with administrator scopes and native host
   support. Tools/environment readiness is separate from host capability.
3. Worker sends bounded heartbeats (suggestion: 15 seconds). Controller marks offline
   after three missed intervals. Queued assignment requires an online compatible
   worker; unavailable targets remain unsupported, not locally ready.
4. Controller routes native local jobs first, existing supported macOS cross-arch jobs
   second, explicit remote jobs third. Linux Flutter x64-to-ARM remains unsupported.
   No public test API is a default dependency.
5. Jobs carry unpredictable UUIDs, immutable source revision, known target IDs,
   sanitized portable config/assets, assignment, creation/expiry and an atomic lease.
   No arbitrary command, executable, shell snippet, filesystem destination or upload
   filename from the job may become an execution/path argument.
6. Worker starts its loopback DVForge backend with the existing `--with-app` primitive;
   users do not open another machine's browser. Installation privileges are the
   worker administrator's policy, not an unchecked controller job field.
7. Stream logs using monotonically increasing sequence numbers with resume cursors,
   bounded chunks and redaction. Existing progress/status polling can provide a
   compatibility adapter, but log_tail is not complete resumable log streaming.
8. Results include assigned targets, statuses, artifact OS/architecture, source/profile
   fingerprint, size and SHA-256. Validate package/executable on the native worker;
   controller verifies immutable job binding and checksum before publishing outputs.
   Write to temporary files then atomically promote after validation. A checksum alone
   authenticates neither the worker nor the metadata.
9. Cancellation is authenticated, job-specific and propagated to the local backend.
   Expired leases/worker loss produce explicit states, not infinite waits or duplicate
   success. Use separate queue, compile, upload and total timeouts. Requeueing must
   not re-use results from a previous lease.
10. HTTPS with ordinary certificate verification is mandatory outside loopback;
    reverse-proxy configuration is administrator-controlled. For shared-folder
    deployments, authenticated signed envelopes plus restricted encrypted share
    transport are required; an SMB mount alone is not worker authentication.

## Current gaps

Shared queue token, permissive legacy file jobs, no authenticated capability
registry, no per-worker token scopes, no unified GUI scheduler, incomplete cancellation,
no reliable offline lease handling, no full resumable remote logs, and no signed
metadata/immutable source binding. Controller-wide packet/checksum architecture
verification also needs a portable DEB/RPM parser or native validation attestation
when the controller lacks those package tools.

The limited transport/checksum hardening in this branch does not solve these gaps.
Do not expose the legacy service as a production internet build controller.

## Required tests before enabling remote targets

Native/local and two mocked workers; cross-OS dispatch; ARM worker selection;
expired heartbeat; capability/version mismatch; unauthorized submit/claim/upload;
cross-worker token reuse; malicious filenames/paths/job fields; bad certificate;
checksum/size mismatch; wrong architecture; duplicate claims and stale lease;
cancel before claim/during build/upload; reconnecting log cursor; partial targets;
worker restart; controller restart; real private LAN job on each supported OS.
