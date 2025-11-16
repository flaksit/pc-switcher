---
description: Resolve feedback received on a speckit feature plan
---

## User Input

```text
$EXTRA_INSTRUCTIONS
```

You **MUST** consider the user input before proceeding (if not empty). The user input takes precedence over any conflicting instructions in this prompt.


We use speckit as workflow to implement features in this project. The feature we are currently working has a specific feature folder under specs/ with the same name as the current branch. In the feature folder, there is a spec.md file that contains the specification of the feature.

You just finished the planning phase for the feature. My quality assurance analyst reviewed your plan against the specs. You find their feedback a the file "spec-review.md" in the feature folder. In case there are multiple files with that name (e.g. spec-review-2.md), make sure to read only the one with the highest number.
First, critically read through their feedback. Come back to me and ask me clarifying questions if you think parts of the feedback are not relevant.

Then, when all your questions are answered, fix "spec.md" according to the feedback. Ensure all points raised in the feedback are addressed.


## Overriding remarks, feedback, instructions, if any
The following instructions override any conflicting instructions above or in the review file. If there are no overriding instructions, this section is empty.

$EXTRA_INSTRUCTIONS
