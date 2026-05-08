The prose/schema drift is resolved. [spec/v0/02-data-model.md](/Users/ericalt/Documents/eden/spec/v0/02-data-model.md:65) now explicitly limits wire SHAs to 40-char SHA-1 or 64-char SHA-256, which matches the regexes in [proposal.schema.json](/Users/ericalt/Documents/eden/spec/v0/schemas/proposal.schema.json:37) and [trial.schema.json](/Users/ericalt/Documents/eden/spec/v0/schemas/trial.schema.json:36).

Final recommendation: **go**. I do not see any remaining review findings from this pass.
