---
title: 'Terraform: change → propose → approve → conductor applies'
when_to_use: An infrastructure change is needed in a repo listed under the bot's terraform.repos.
prerequisites:
- 'terraform: configured for the bot'
- the change pushed as a commit
steps:
- Check the infrastructure repo out in your working directory, on a new branch.
- Make one change; terraform fmt and validate locally if you can.
- Commit, push (git.push, or ask your operator), and note the full SHA.
- Propose it with conductor bot propose-terraform, naming the root module.
- Read the plan conductor made; every replace or destroy must be expected.
- Tell your operator the summary, why, the risk and the rollback.
- conductor bot wait; on a stale plan, conductor bot replan and read the new plan.
- Verify the change, record the outcome, and remember what you learned.
verification: The applied change is visible in the target system and the case records the outcome.
rollback: Propose a revert commit the same way; conductor plans and applies it after approval.
owner: the bot's operator
---
# Terraform: change → propose → approve → conductor applies

You never run `terraform apply` and you hold no write role. You propose a
**commit**; conductor plans it itself, your operator approves that exact plan,
and conductor applies it.

1. Work in your own working directory. Check out the infrastructure repo there
   (a repo your operator listed under the bot's `terraform.repos`), on a
   new branch.
2. Make the change. Keep it to one purpose. `terraform fmt` and
   `terraform validate` locally if you can.
3. Commit it with a message that says what and why, and push the branch
   (`conductor call git.push`; until that verb exists, ask your operator to
   push it for you). Note the **full** commit SHA.
4. Propose it, naming the root module it applies to:

       conductor bot propose-terraform --repo infra --commit <full sha> --dir envs/prod \
           [--workspace prod] [--var-file envs/prod/prod.tfvars] --summary "why, in one line"

   Repo, commit, directory, workspace, var files and targets are all part of
   what is approved; none of them can change afterwards.
5. Conductor checks the commit out in its own directory and plans it with a
   read role. It tells you when the plan is ready; `conductor bot
   proposals` shows the full plan text. **Read it.** Every resource that is
   replaced or destroyed must be expected. If anything surprises you, tell your
   operator to deny it and say why.
   - A plan that destroys or replaces a stateful resource (databases, buckets,
     disks, keys…) is **refused** by conductor unless your operator explicitly
     allowed that address. Do not try to work around it: report it, with the
     reason the destroy is needed and how data is preserved.
6. Tell your operator: the summary line (`+N ~M -K`, anything replaced or
   destroyed), why, the risk, and how to roll back. They approve with a signed
   `conductor bot approve`, which covers that plan's hash and nothing else.
7. `conductor bot wait <proposal>` returns once it is applied, refused or
   denied. If it failed because the plan went **stale** (the state changed
   since planning), `conductor bot replan <proposal>` plans the same commit
   again as a new proposal — read the new plan before recommending it.
8. Verify the change took effect, record the outcome in the case, and write
   anything learned to `knowledge/`.
