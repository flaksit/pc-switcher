---
description: Resolve feedback on a speckit feature plan
---

## User Input

```text
$EXTRA_INSTRUCTIONS
```

You **MUST** consider the user input before proceeding (if not empty). The user input takes precedence over any conflicting instructions in this prompt.


We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a "spec.md" file that contains the specification of the feature.

You just finished the planning phase for the feature. My quality assurance analyst reviewed your plan against the specs. You find their feedback in the file "plan-review-N*.md" in the feature folder. Make sure to read only the file with the highest number N.
First, critically read through their feedback. For each point in the feedback, ensure you understand the underlying issue. If you disagree with any point, discuss it with me. Come back to me and ask clarifying questions or ask for deciding if needed (e.g. between multiple possible solutions).

Then, when all your questions are answered, fix your plan according to the feedback. Ensure all points raised in the feedback are addressed and that the plan fully complies with the specifications outlined in "spec.md".

### Output
Write a report to a new file "plan-review-N-resolution.md", where N is the same number as of the review file you read. The goal is to have a trace of any decisions we make while resolving the feedback. The report should contain the following:
- $EXTRA_INSTRUCTIONS
- A log of any further conversation we have while you are resolving the feedback. This will mainly be answers to your clarifying questions, but we could get into deeper conversation about some of the feedback given. Just literally capture your output to me and my prompts.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above or in the review file. If there are no overriding instructions, this section is empty.

$EXTRA_INSTRUCTIONS
