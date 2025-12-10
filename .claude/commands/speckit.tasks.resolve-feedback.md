---
description: Resolve feedback received on a speckit feature tasks list
---

We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a "tasks.md" file that contains the actionable task list for the feature.

You just finished the tasks phase for the feature. My quality assurance analyst reviewed your tasks against the spec and plan. You find their feedback in the file "tasks-review-N*.md" in the feature folder. Make sure to read only the file with the highest number N.

First, critically read through their feedback. For each point in the feedback, ensure you understand the underlying issue. If you disagree with any point, discuss it with me. Come back to me and ask clarifying questions or ask for deciding if needed (e.g. between multiple possible solutions).

Then, when all your questions are answered, fix your tasks according to the feedback. Ensure all points raised in the feedback are addressed and that the tasks:
- Fully comply with the specifications outlined in "spec.md"
- Are consistent with the implementation plan in "plan.md"
- Are properly ordered by dependencies
- Are actionable and well-scoped

### Output
Write a report to a new file "tasks-review-N-resolution.md", where N is the same number as of the review file you read. The goal is to have a trace of any decisions we make while resolving the feedback. The report should contain the following:
- The literal ARGUMENTS text passed to you when invoking this command.
- A log of any further conversation we have while you are resolving the feedback. This will mainly be answers to your clarifying questions, but we could get into deeper conversation about some of the feedback given. Just literally capture your output to me and my prompts.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above or in the review file. If there are no overriding instructions, this section is empty.

$ARGUMENTS
