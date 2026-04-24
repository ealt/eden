**Findings**
- Bug — [03-roles.md](/Users/ericalt/Documents/eden/spec/v0/03-roles.md:333), §4.4 now says `completed_at` is “set to the time of the terminal trial transition,” including “the retry-exhausted `eval_error` transition.” But the frozen trial model still says in [02-data-model.md](/Users/ericalt/Documents/eden/spec/v0/02-data-model.md:302), §7.1 that `completed_at` means “When the evaluator submitted.” Those are no longer equivalent once retry exhaustion or operator abandonment can happen after the evaluator’s last submit.

**Assessment** That `completed_at` mismatch is the only remaining blocker I see. The new `eval_error` persistence rule itself is good and closes the prior ambiguity.

If you update the frozen field description in `02 §7.1` to match the new terminal-transition semantics, I’d consider this review chunk ready to merge.
