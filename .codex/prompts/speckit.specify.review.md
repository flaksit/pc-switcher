---
description: Review a speckit feature specification
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My analyst just wrote a detailed specification for the feature. You can find it in the file "spec.md" in the feature folder.
Review their specification on completeness, correctness, consistency. Give any other valuable feedback or ideas for substantial improvements you might have. Don't change the specification itself. Write your feedback to a new file "spec-review-N.md" in the feature folder so I can feed your feedback to the analyst to fix/improve the specification. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).

**Scope boundary:** The specification phase defines *what* the system should do, not *how*. Keep feedback technology-agnostic:

- Requirements should describe user outcomes and system behaviors
- Avoid requesting specific tools, commands, file formats, or technical mechanisms
- If a requirement implies a technology choice, flag it as premature—don't suggest a different technology

Examples of feedback that belongs in spec review:
- "The requirement for VM reset doesn't specify what 'clean baseline' means to the user"
- "Missing: what should happen if two developers try to run tests simultaneously?"

Examples of feedback that belongs in *plan* review (not here):
- "Should use btrfs snapshots instead of reimaging"
- "Specify the pytest markers and env vars"
- "Define the lock file format and TTL"

If you find yourself suggesting specific technologies or implementation details, stop—that feedback is for the planning phase.

**Materiality guidance:** Only raise issues that would cause implementation problems:
- Missing or ambiguous requirements that would block developers
- Internal contradictions between sections
- Scope unclear enough to cause divergent interpretations
- Conflicts with the constitution or ADRs

Do NOT raise issues about:
- Wording preferences or stylistic choices
- "Nice to have" additions that aren't needed for v1
- Theoretical edge cases the spec explicitly defers

**If the spec is sufficient to implement the feature correctly:** Say so. A brief confirmation that requirements are clear and complete is a valid review outcome.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.

$ARGUMENTS
