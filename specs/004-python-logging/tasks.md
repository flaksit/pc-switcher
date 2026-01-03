# Tasks: Standard Python Logging Integration

**Input**: Design documents from `/specs/004-python-logging/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, quickstart.md

**Tests**: Tests will be part of polish phase for regression validation.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Path Conventions

- **Single project**: `src/pcswitcher/`, `tests/` at repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project structure preparation and ADR documentation

- [X] T001 Create ADR-010 for logging infrastructure decision in docs/adr/adr-010-logging-infrastructure.md
- [X] T002 [P] Create ADR-010 considerations document in docs/adr/considerations/adr-010-logging-infrastructure-analysis.md
- [X] T003 Update docs/adr/_index.md to include ADR-010 reference

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core logging infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Update LogLevel enum values in src/pcswitcher/models.py to align with stdlib (10, 15, 20, 30, 40, 50)
- [X] T005 Register custom FULL log level (15) with stdlib logging in src/pcswitcher/logger.py
- [X] T006 Create LogConfig dataclass for 3-setting model (file, tui, external) in src/pcswitcher/config.py
- [X] T007 Add logging section to config schema in src/pcswitcher/schemas/config-schema.yaml
- [X] T008 Implement LogConfig parsing with validation and defaults in src/pcswitcher/config.py (FR-009: defaults, FR-010: raise ConfigurationError on invalid level strings)

**Checkpoint**: Foundation ready - user story implementation can now begin in parallel

---

## Phase 3: User Story 3 - Migrate Internal Logging to Standard Library (Priority: P2)

**Goal**: Replace custom Logger/EventBus logging with Python's standard logging module

**Why first**: This is the architectural foundation that enables US1, US2, and US4 to work. Despite being P2, it must be implemented before the other stories.

**Independent Test**: Run any module that logs and verify messages flow through stdlib logging infrastructure.

### Implementation for User Story 3

- [X] T009 [US3] Implement QueueHandler/QueueListener setup in src/pcswitcher/logger.py
- [X] T010 [US3] Register atexit handler for QueueListener.stop() in src/pcswitcher/logger.py
- [X] T011 [US3] Create setup_logging() function that configures root and pcswitcher loggers in src/pcswitcher/logger.py
- [X] T012 [US3] Implement logger hierarchy: root level=external, pcswitcher level=min(file,tui) in src/pcswitcher/logger.py
- [X] T013 [US3] Update cli.py main callback to call setup_logging() with LogConfig in src/pcswitcher/cli.py
- [X] T014 [US3] Deprecate LogEvent class with comment in src/pcswitcher/events.py (keep for reference, mark deprecated)

**Checkpoint**: At this point, logging flows through stdlib infrastructure

---

## Phase 4: User Story 4 - Preserve Current Log Format and Features (Priority: P2)

**Goal**: Maintain identical JSON file format and Rich TUI format after migration

**Independent Test**: Compare log output before and after migration for visual consistency.

### Implementation for User Story 4

- [X] T015 [P] [US4] Implement JsonFormatter class in src/pcswitcher/logger.py with same JSON structure as FileLogger (FR-011: include all extra dict key-value pairs as context fields)
- [X] T016 [P] [US4] Implement RichFormatter class in src/pcswitcher/logger.py with same Rich output as ConsoleLogger (FR-011: append extra dict key-value pairs as dim text)
- [X] T017 [US4] Handle optional job/host context in formatters (omit when missing, not empty brackets); generic extra fields always appended in src/pcswitcher/logger.py
- [X] T018 [US4] Wire formatters to FileHandler and StreamHandler in setup_logging() in src/pcswitcher/logger.py

**Checkpoint**: Log output format is preserved (JSON lines for file, Rich for TUI)

---

## Phase 5: User Story 1 - Configure Log Levels in Config File (Priority: P1)

**Goal**: Users can specify file, tui, and external log levels in config.yaml

**Independent Test**: Create config with specific levels, run sync, verify file has debug messages TUI doesn't show.

### Implementation for User Story 1

- [X] T019 [US1] Pass LogConfig from Configuration to setup_logging() in src/pcswitcher/cli.py
- [X] T020 [US1] Apply file level to FileHandler in setup_logging() in src/pcswitcher/logger.py
- [X] T021 [US1] Apply tui level to StreamHandler in setup_logging() in src/pcswitcher/logger.py
- [X] T022 [US1] Update README.md with logging configuration section

**Checkpoint**: Users can configure log levels via config file

---

## Phase 6: User Story 2 - View External Library Logs (Priority: P1)

**Goal**: See asyncssh and other external library log messages in file and TUI output

**Independent Test**: Trigger SSH connection warning, verify it appears in both outputs when levels permit.

### Implementation for User Story 2

- [X] T023 [US2] Set root logger level to external_level in setup_logging() in src/pcswitcher/logger.py
- [X] T024 [US2] Verify asyncssh logs are captured when external level is DEBUG/INFO in integration test scenario

**Checkpoint**: External library logs appear in output when levels permit

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Cleanup, migration completion, and validation

- [X] T025 [P] Remove unused Logger, FileLogger, ConsoleLogger classes from src/pcswitcher/logger.py
- [X] T026 Update module __all__ exports in src/pcswitcher/logger.py
- [X] T027 Update orchestrator.py to use stdlib logging instead of custom Logger class in src/pcswitcher/orchestrator.py
- [X] T028 [P] Update job modules to use stdlib logging (getLogger pattern): src/pcswitcher/jobs/base.py, btrfs.py, context.py, disk_space_monitor.py, dummy_fail.py, dummy_success.py, install_on_target.py
- [X] T029 Update ui.py TUI log consumption to work with new logging pipeline in src/pcswitcher/ui.py
- [X] T030 [P] Create unit tests for logging setup and filtering in tests/unit/test_logging.py (covers SC-003, SC-004, SC-007, SC-008; includes test case for invalid log level causing ConfigurationError per FR-010)
- [X] T031 [P] Create contract tests for log format in tests/contract/test_logging_contract.py (covers SC-005: TUI visual format, SC-006: JSON structure)
- [X] T032 Run existing test suite (`uv run pytest`) to verify no regressions (SC-007); all tests must pass before proceeding
- [X] T033 Validate quickstart.md scenarios work as documented

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies - can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion - BLOCKS all user stories
- **User Story 3 (Phase 3)**: Must complete first despite P2 priority (architectural foundation)
- **User Story 4 (Phase 4)**: Depends on Phase 3 (needs handlers to attach formatters)
- **User Story 1 (Phase 5)**: Depends on Phases 3 & 4 (needs working handlers)
- **User Story 2 (Phase 6)**: Depends on Phases 3 & 4 (needs working handlers)
- **Polish (Phase 7)**: Depends on all user stories being complete

### User Story Dependencies

- **User Story 3 (Migrate to stdlib)**: Can start after Foundational - Foundation for all other stories
- **User Story 4 (Preserve format)**: Depends on US3 - Needs handlers to exist
- **User Story 1 (Config levels)**: Depends on US3/US4 - Needs complete logging pipeline
- **User Story 2 (External logs)**: Depends on US3/US4 - Needs complete logging pipeline
- **Note**: US1 and US2 can run in parallel after US4 completes

### Within Each User Story

- Core infrastructure before configuration wiring
- Handlers before formatters
- Formatters before level filtering
- Internal before external library handling

### Parallel Opportunities

- T001 and T002 can run in parallel (different ADR files)
- T015 and T016 can run in parallel (different formatter classes)
- T025, T028, T030, T031 can run in parallel (different files)
- US1 and US2 can run in parallel after US4 completes

---

## Parallel Example: Phase 4

```bash
# Launch formatter implementations together:
Task: "Implement JsonFormatter class in src/pcswitcher/logger.py"
Task: "Implement RichFormatter class in src/pcswitcher/logger.py"
```

---

## Implementation Strategy

### Incremental Delivery with Validation Checkpoints

All user stories and requirements from spec.md are included. The phases below define **validation checkpoints** where you can verify progress before continuing.

1. Complete Setup + Foundational -> ADR written, LogConfig ready
2. Add User Story 3 -> **Checkpoint**: stdlib integration works
3. Add User Story 4 -> **Checkpoint**: log format preserved (can validate visually)
4. Add User Story 1 -> **Checkpoint**: users can configure levels via config file
5. Add User Story 2 -> **Checkpoint**: external library logs captured
6. Complete Polish -> **Done**: cleanup, tests, documentation complete

Each checkpoint adds value without breaking previous work.

### Single Developer Strategy

Recommended sequence for single developer:

1. Phase 1 (Setup): Create ADR documentation
2. Phase 2 (Foundational): LogLevel + LogConfig
3. Phase 3 (US3): Core stdlib migration
4. Phase 4 (US4): Formatters
5. Phase 5 (US1): Config wiring
6. Phase 6 (US2): External library capture
7. Phase 7 (Polish): Cleanup + tests

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- US3 is implemented before US1/US2 despite lower priority because it's architectural foundation
- Existing Logger/FileLogger/ConsoleLogger are replaced, not extended
- EventBus remains for ProgressEvent and ConnectionEvent (non-logging events)
- atexit handler ensures QueueListener cleanup on all exit paths
- LogEvent is deprecated but kept for reference until migration is verified complete
- Commit after each task or logical group
