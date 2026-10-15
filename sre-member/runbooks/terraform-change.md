# Terraform: change → propose → approve → conductor applies

You never run `terraform apply` and you hold no write role. You propose a
**commit**; conductor plans it itself, your operator approves that exact plan,
and conductor applies it.

1. Work in your own working directory. Check out the infrastructure repo there
   (a repo your operator listed under the member's `terraform.repos`), on a
   new branch.
2. Make the change. Keep it to one purpose. `terraform fmt` and
   `terraform validate` locally if you can.
3. Commit it with a message that says what and why, and push the branch
   (`conductor call git.push`; until that verb exists, ask your operator to
   push it for you). Note the **full** commit SHA.
4. Propose it, naming the root module it applies to:

       conductor member propose-terraform --repo infra --commit <full sha> --dir envs/prod \
           [--workspace prod] [--var-file envs/prod/prod.tfvars] --summary "why, in one line"

   Repo, commit, directory, workspace, var files and targets are all part of
   what is approved; none of them can change afterwards.
5. Conductor checks the commit out in its own directory and plans it with a
   read role. It tells you when the plan is ready; `conductor member
   proposals` shows the full plan text. **Read it.** Every resource that is
   replaced or destroyed must be expected. If anything surprises you, tell your
   operator to deny it and say why.
   - A plan that destroys or replaces a stateful resource (databases, buckets,
     disks, keys…) is **refused** by conductor unless your operator explicitly
     allowed that address. Do not try to work around it: report it, with the
     reason the destroy is needed and how data is preserved.
6. Tell your operator: the summary line (`+N ~M -K`, anything replaced or
   destroyed), why, the risk, and how to roll back. They approve with a signed
   `conductor member approve`, which covers that plan's hash and nothing else.
7. `conductor member wait <proposal>` returns once it is applied, refused or
   denied. If it failed because the plan went **stale** (the state changed
   since planning), `conductor member replan <proposal>` plans the same commit
   again as a new proposal — read the new plan before recommending it.
8. Verify the change took effect, record the outcome in the case, and write
   anything learned to `knowledge/`.
