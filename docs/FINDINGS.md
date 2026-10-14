# FINDINGS

Verified API behavior and open questions. Every entry in "Verified behavior" needs: date,
endpoint, what we expected, what actually happened, and a doc link if any.

## Verified behavior

_Nothing verified yet._

<!-- Entry template:
### <short title>
- Date:
- Endpoint:
- Expected:
- Actual:
- Probe output: probes/out/<name>.json
- Docs:
-->

## Open questions

- [ ] How to break permission inheritance on a folder/file: Graph may not support it directly;
  SharePoint REST `breakroleinheritance` likely needed (requires certificate app-only auth).
- [ ] How Entra security groups appear as SharePoint principals (claims format) and how to grant
  them access via REST role assignments.
- [ ] Whether Graph `driveItem` permissions show inherited vs. unique grants, and how.
- [ ] How SharePoint groups (site Owners/Members/Visitors) and special claims such as
  "Everyone except external users" appear in permission responses.
- [ ] What happens to a file's unique permissions when it is moved to another folder.
- [ ] Whether unlicensed users can be granted access and be trimmed correctly in Azure AI Search.
- [ ] Which token audience/scope the Azure AI Search ACL query requires for the end user.
