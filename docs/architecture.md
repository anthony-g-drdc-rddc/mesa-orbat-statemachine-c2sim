# Implementation and operating notes

## Ownership

`runtime.Coordinator` is the only authoritative state writer. Each command or simulation boundary starts from a detached SQLAlchemy checkpoint. All service and Mesa effects change this candidate only. A compare-and-swap transaction inserts the next complete checkpoint and updates the run head together. Readers load committed checkpoints. A failed transaction or resolver leaves the prior boundary intact.

`domain/` contains pure predicates, lifecycle states, audit helpers and graph validation; it imports no HTTP, SQL or ORBAT code. `contracts/` validates public JSON. `services/` implements six coordinating services and four tasking services sharing one lifecycle. `simulation/` implements a Mesa model and physical agents. `orbat_io/` owns native parsing, projection and export.

The coordinator accepts its random-provider factory through construction. Decision application depends on the `DecisionProvider` protocol. Only the seeded random implementation is configured or shipped. Every call sorts immutable options by their canonical intent hash and uses one uniform `randrange`. A linked option contains the mission intent and all affected task dispositions. All intents are checked before any effect applies. Persistence snapshots and restores both the decision RNG and Mesa's Python/NumPy generators.

## Boundaries and communication

Each 1000 ms interval delivers due serializable messages, updates service knowledge, generates the command-level choice from delivered evidence, applies the selected atomic alignment, checks physical conditions, collects all agent proposals, resolves them in stable unit-ID order, schedules later reports, and commits. The input's `commit_seq` identifies the committed boundary from which this candidate was derived; its evidence IDs also identify messages admitted in this boundary's delivery phase. Inputs, guard results and outputs are retained together with that boundary. A decision ID includes the option/evidence fingerprint so a delivery-phase revision cannot reuse an earlier input's identity.

Instructions, acknowledgement reports, acceptance, actual start and reported start are separate. Actual completion precedes its delivered completion report. Messages always wait at least one delivery phase, even if configured delay is zero. The task state exposed by C2 is the reported lifecycle; installed task versions and actual results are separate physical records. Versioned reports can update historical executions, never a replacement task.

A stopped or paused run never ticks automatically. An explicit `step` executes one interval in READY, RUNNING or PAUSED. A mission pause has different semantics: new starts are prohibited immediately, while active elements obey pause instructions when delivered. Cancellation cannot overwrite an actual completion. Terminal missions drain task confirmations before the run becomes terminal.

Organizational nodes remain records. Physical agents implement route movement, wait, inspect, collect, load, unload and deliver. Transfers require co-location, available stock, and a unique version-scoped transfer ID. Fuel is consumed only during active execution. Reservations remain held until an old physical activity stops; a replacement instruction retries while waiting. Routes can have declared timed physical changes; a blocked task returns to its prior reported lifecycle after clearance and guard checks.

## Scenario and planning scope

ORBAT supplies unit identity, hierarchy, geography and native resources. The strict C2 sidecar supplies domain assignments, speeds, criteria, plans, dependencies, timings and environmental conditions. Plans are declarative finite alternatives. This release validates and selects the next eligible supplied plan version; it does not synthesize routes or perform automated planning. An approved plan is retained unchanged. Reapproval can reuse that version. A changed assignment needs a higher task content version; unchanged versions retain their authorization and progress.

The six predicate operators are `all_of`, `any_of`, `task_completed`, `report_confirmed`, `supply_received` and `element_accounted_for`. They inspect delivered records only. Collection findings are not automatically confirmed. Simulator-issued receipts and accountability reports are authoritative evidence in this toy model.

The supplied fixture uses short routes and accelerated speeds to keep automated verification quick. Its physical behaviors are a research demonstrator, not a validated representation of platform performance. It has no combat-resolution or commander-reasoning model.

## Persistence and replay

The schema stores run heads and immutable full checkpoints as SQLAlchemy JSON records. This keeps service records, audit logs and decisions transactionally consistent without a partially persisted event projection. It also intentionally duplicates historical data. Long-running/high-volume simulations would need snapshot retention and indexed event tables in a later migration.

A restored child keeps the exact physical, service, queue and random state plus a parent pointer. Its branch-local control sequence starts at zero. The child starts READY so the caller explicitly starts continuation. Recorded playback simply loads retained checkpoints. `verify` re-executes recorded controls and choices and compares complete boundaries, including output indices and random states; it fails on divergence. Infrastructure-failure-only boundaries cannot be re-executed as simulation actions.

SQLite is configured for one process/worker and one writer. The filesystem lock is released by the OS if the process dies. Do not place this database on a filesystem that does not correctly implement SQLite locks. Alembic migrations run before the API accepts work.

## ORBAT compatibility verification

The adapter was checked against upstream commit `d7d3a5cde9996549a18cff0fc8a1cc10421f6a62`, specifically `scenarioModels.ts`, `scenarioLayerItems.ts`, `scenarioCodec.ts`, `unitResources.ts` and the native upgrade path.

Both the source fixture and an exported completed scenario were passed through the unchanged upstream `upgradeScenarioIfNecessary`, `unitResourcesToInternal`, `unitStateToInternal`, `applyResourceUpdate` and `applyResourceDiff` functions in an isolated Node harness. The checks retained eight units and four routes and recovered fifty supplies at POST-1 from the exported timed resource states.

A full upstream browser build was not run: its lockfile contains an `xlsx` tarball without an integrity hash, which the package manager's supply-chain policy rejected. The narrower native-format harness excluded that unrelated spreadsheet dependency and did not change ORBAT source or relax the policy. Python acceptance tests additionally verify identifiers, hierarchy, resources, projection tolerance, timeline export and original source bytes. Visual browser import remains a manual acceptance check.

## Deployment verification scope

The CLI, in-process API, SQL persistence, process replacement, checkpoint continuation and native format conversions are exercised by tests. Docker/Compose definitions are provided; building a container requires an available Docker daemon. The development environment used for implementation did not provide one.

To repeat the optional upstream native-format check, create a separate temporary Node project with `esbuild@0.25.11`, `compare-versions@6.1.1`, `@orbat-mapper/control-measures@0.32.0`, `vue@3.5.22`, `nanoid@5.1.6` and `klona@2.0.6`. From that directory, run `node /path/to/this/repo/scripts/verify_orbat_native.mjs /path/to/orbat-mapper /path/to/source.orbat.json /path/to/exported/scenario.orbat.json`. The harness bundles and invokes unchanged upstream modules; it does not install the full application or its spreadsheet importer.
