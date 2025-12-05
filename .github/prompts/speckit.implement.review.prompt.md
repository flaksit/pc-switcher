---
description: Review a speckit feature implementation
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

My developer just implemented the feature.
Review the implementation. Verify if the current state of the project implements the spec completely and correctly. Write your feedback to a new file named "implementation-review-N.md" in the feature folder so I can feed it back to my developer so they can fix their work. Replace N in the filename with the next available number starting from 1 (because there could be multiple reviews).
If there is already a file "implementation-review.md", create a new file "implementation-review-2.md", and so on. Number all feedback points in the file so it is easy to reference them.

**Materiality guidance:** Only raise issues where:
- A spec requirement is not implemented or implemented incorrectly
- Code doesn't work as designed (bugs, logic errors)
- Tests don't cover the acceptance scenarios from the spec
- Violations of constitution or project coding standards

Do NOT raise issues about:
- Code style preferences beyond established project standards
- Refactoring opportunities unrelated to the feature
- "I would have done it differently" suggestions

**If the implementation is complete and correct:** Say so clearly. Example:
> ## Review Summary
> The implementation is **COMPLETE** and meets spec requirements. No critical issues found.
>
> ### Optional observations (non-blocking)
> - [Minor suggestions, if any, explicitly marked as optional]


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above. If there are no overriding instructions, this section is empty.
