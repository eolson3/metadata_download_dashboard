# metadata_download_dashboard

Downloading OSF Project Activity Logs, Wikis, and Metadata

You can download the activity logs, wikis, and metadata for your OSF Projects and Components via the OSF API, utilizing an available python script, terminal/command line commands, and local hosted dashboard. This may be a valuable asset if you are moving your OSF content and continuing a project in a separate repository. 

This guide will help you download the following for components that you have access to:
Activity log data - including the date, action type, affected project or component, and other details supplied with each event. Includes a json and csv format.
Project wikis - all versions of the project wikis in the markdown format.
Project metadata - The metadata associated with the project or component, including title, contributors, description, license, and more.

The export preserves the project and component hierarchy. Folder and file names include both the OSF title and GUID.

Project files are not downloaded by this version of the checklist. See the guides on downloading project files or using the osfclient.


Step 1: Create a personal access token to access private projects

1. Sign in to the OSF.
2. On the left-hand side of the screen, click “Settings”, then select “Personal Access Tokens”.
3. Select “Create Token”

4. Enter a name for the token, such as `Wiki download`.
5. Select the `osf.full_read` scope. Write access is not required.

6. Create the token, then copy it immediately. The OSF displays the token only once.


Treat the token like a password. Do not place it in a script, include it in a screenshot, or share it with anyone. You can delete the token from your OSF settings when the download is complete if you wish.

For more information, see [Profile and Account: Create a Personal Access Token](https://help.osf.io/article/390-profile-and-account#create-a-personal-access-token).


Step 2: Install or check Python

On macOS, open Terminal and run (type the following text and hit enter):

python3 --version


On Windows, open PowerShell and run:

py --version


If the command displays Python 3 followed by a version number (e.g. if it says something like, “Python 3.9.6”), continue to the next section.

If Python is not installed, download Python 3 from [python.org](https://www.python.org/downloads/). On Windows, select the option to add Python to your PATH when running the installer. After Python is installed, attempt Step 2 again.



Step 3: Download the dashboard script

The script is available on GitHub, named `osf_export_checklist_metadata_all_projects_v0_10.py` (temporary: https://github.com/eolson3/metadata_download_dashboard/blob/main/osf_export_checklist_metadata_all_projects_v0_10.py). Go to this page and click the “download raw file” image near the top right.



Step 4: Confirm that you have the current script

This guide applies to version v0_10.

osf_export_checklist_metadata_all_projects_v0_10.py 0.10-no-empty-summary-folders (selectable metadata summaries, full
metadata archives, current wikis, wiki histories, and activity logs; project files disabled)


On macOS, run the following to check:


cd ~/Downloads
python3 osf_export_checklist_metadata_all_projects_v0_10.py --version

Your Mac may ask you to confirm it has permissions to access your Downloads folder. Select “allow” to continue.

On Windows:


cd $HOME\Downloads
py .\osf_export_checklist_metadata_all_projects_v0_10.py --version


The result should include:


osf_export_checklist_metadata_all_projects_v0_10.py 0.10-no-empty-summary-folders (selectable metadata summaries, full
metadata archives, current wikis, wiki histories, and activity logs; project files disabled)


If it reports a different version, make sure you are running the newly downloaded file rather than one with a similar name.


Step 5: Start the checklist

This command includes the “Downloads” folder as the destination for the exports. You can change the destination folder in the path for each script below if you prefer. See Choose another export location.


On macOS run the following:


cd ~/Downloads
python3 osf_export_checklist_metadata_all_projects_v0_10.py


On Windows, run the following:

cd $HOME\Downloads
py .\osf_export_checklist_metadata_all_projects_v0_10.py

The terminal will tell you that project file downloads are disabled and prompt you to paste your OSF token when prompted. The token will not appear on screen while you paste or type it. That is expected.

If you paste your token correctly, the script will confirm who you’ve been logged in as and will begin checking your account. The script retrieves the projects and components accessible to your account, reconstructs their hierarchy, and opens the checklist in your browser. An account with many projects may take several minutes to load.

You must keep the Terminal or PowerShell window open while using the checklist. You can stop and re-run the command to update the checklist.   You can export a CSV of your checklist if you would like to keep a version of it offline.

If the browser does not open automatically, copy the local address printed in the terminal. It will resemble:


http://127.0.0.1:8765/


Step 6: Using the dashboard to download single project information

The dashboard lists top-level projects and their components. It includes search and filters for visibility and review status.

Each top-level project has five export buttons:

Comprehensive metadata - exports all available metadata for the project and every accessible component beneath it, included as a separate json for each API response. Comprehensive but hefty.
Metadata summary - exports a DataCite schema-inspired, human-readable project/component catalog plus combined CSV/JSON of critical fields. Preferable if you do not need records of all possible metadata and relationships on a project.
Wikis + history - exports current wiki pages and all available versions. 
Current wikis only - exports current wiki pages.
Activity logs - exports activity records in CSV and JSON.


Exports run in the background, one project tree at a time, so you can do other things while you wait for your files to download as long as the terminal window and dashboard stay open. You can run each export for a project separately and all exports will be included in the same folder in your download destination (called “OSF Project Exports”). If you have a large number of projects/components, downloading everything may take some time.

Keep the terminal window until the jobs finish. You may also wish to keep the dashboard open to monitor progress.



Step 7: Track completed projects

The checkbox to the left of each project or component is a review control. Use it to mark items that you have reviewed or downloaded.

Review checkmarks are saved locally and restored when you run the checklist again, provided that you use:

- The same OSF account
- The same export folder

The saved state is stored in a hidden file inside the export folder:


.checklist-state-[OSF account ID].json


The checklist does not automatically mark a project as reviewed after an export. You control the review checkmarks.

The “Include in batch” selections are temporary and are not restored the next time the dashboard runs.

Use “Clear review checks” only when you want to remove every saved review checkmark.



Step 8: Export several projects

To create a batch:

1. Select “Include in batch” to the right of each desired top-level project.
2. Choose one of the “Selected” export buttons in the Batch export section.

You can also use:

- “Select all projects” to include every top-level project tree.
- “Clear batch selection” to remove every project from the current batch.

Each selected top-level project becomes a separate job with its own output folder, with components included within. If you have many projects, selecting all could take several hours depending on project complexity and your internet connection. You do not need to remain on the browser page or keep this page open, but the terminal must remain active. Recommended to limit to 10 at a time and use the review control to check off projects you have downloaded, especially if you are exporting “Everything”.


What you can export

Full metadata archive export

If the full metadata archive is chosen, each project or component has its own “Full Metadata Archive” folder:


Full Metadata Archive/
├── Project Title [abc12] - Complete Metadata.html
├── Project Title [abc12] - Complete Metadata.json
└── API Responses/
 ├── Project Title [abc12] - Core API Response.json
 ├── Project Title [abc12] - Contributors.json
 ├── Project Title [abc12] - Custom Metadata.json
 ├── Project Title [abc12] - CEDAR Metadata.json
 └── additional related metadata records

The comprehensive metadata export follows linked OSF API relationships instead of saving only the basic project response. When available, it includes:

- Title, GUID, URL, category, description, tags, subjects, visibility, and dates
- Contributors, bibliographic status, permissions, profiles, and identifiers
- Affiliated institutions
- OSF, DOI, and other identifiers
- License and rights information
- Language and resource type
- Funders, awards, and grants
- Registrations, preprints, and linked OSF resources
- Storage region and configured storage providers
- Custom metadata and CEDAR metadata records
- Parent and component relationships

Storage-provider records are metadata only. The files stored in those providers are not downloaded.

The full archive HTML provides a readable view of the retrieved metadata for an individual project or component. The matching JSON retains the complete assembled metadata package. Files under API Responses preserve the underlying OSF API records separately for preservation, analysis, or troubleshooting.


Metadata summary export
If the metadata summary is chosen, the top-level project has a “Metadata Summary” folder:
Metadata Summary/
├── Project Title [abc12] - Metadata Summary Catalog.html
├── Project Title [abc12] - Metadata Summary Catalog.json
├── Project Title [abc12] - Project and Component Metadata Summary.csv
└── Project Title [abc12] - Project and Component Metadata Summary.json
The metadata summary creates one consolidated catalog covering the top-level project and all of its exported components. Rather than preserving every available API relationship and source response, it collects a smaller set of descriptive metadata intended for review, reuse, and reporting.
When available, the summary includes:
Title, GUID, URL, category, description, visibility, and dates
Tags and subjects
Bibliographic and other contributors
Contributor permissions and identifiers
Affiliated institutions
OSF, DOI, and other identifiers
License, copyright, and rights information
Language and resource type
Funders, awards, and grants
Parent and component relationships
The HTML catalog is intended for people to read. It includes a table of contents and a concise record for each project or component.
The matching catalog JSON uses DataCite-inspired field names for creators, titles, identifiers, descriptions, subjects, rights, funding, dates, institutions, and related resources. It is inspired by the DataCite schema but is not represented as a validated DataCite deposit record.
The CSV provides a flattened version suitable for spreadsheets. The additional JSON preserves the collected project and component summary records in a structured format.

Complete wiki+history export

For every project and component, the wiki export creates:

- A Markdown file containing the current version of each wiki page
- A “Version History” folder containing every available historical version
- CSV and JSON version-history records

Example:


Wikis/
├── Project Title [abc12] - Wiki - Home [wiki-id].md
└── Version History/
 └── Home [wiki-id]/
  ├── Project Title [abc12] - Wiki - Home [wiki-id] - Version 0001 - date.md
  ├── Project Title [abc12] - Wiki - Home [wiki-id] - Version 0002 - date.md
  ├── Project Title [abc12] - Wiki - Home [wiki-id] - Version History.csv
  └── Project Title [abc12] - Wiki - Home [wiki-id] - Version History.json

Wiki exports can take time when projects contain many pages or long version histories.


Current wiki export

For every project and component, the wiki export creates:

- A Markdown file containing the current version of each wiki page

Example:


Wikis/
├── Project Title [abc12] - Wiki - Home [wiki-id].md
└── Version History/


Understand the activity-log export

Activity logs are written in two formats:

- CSV for spreadsheets and review
- JSON for preserving the complete API records

The script assigns activity entries to the project or component where the activity originated when that information is available.

Logs associated with a deleted, former, or inaccessible component may appear beneath:


Former or inaccessible components/


This preserves activity that might otherwise be difficult to associate with the current project tree.


Export folder structure

By default, exports are written to:

Downloads/OSF Project Exports

Only folders corresponding to the selected export options are created. If all export options are selected, a completed export may resemble:


OSF Project Exports/
└── Project Alpha [abc12]/
 ├── Metadata Summary/
 │ ├── Project Alpha [abc12] - Metadata Summary Catalog.html
 │ ├── Project Alpha [abc12] - Metadata Summary Catalog.json
 │ ├── Project Alpha [abc12] - Project and Component Metadata Summary.csv
 │ ├── Project Alpha [abc12] - Project and Component Metadata Summary.json
 │ └── Project Alpha [abc12] - Metadata Summary Export Summary.json
 │
 ├── Full Metadata Archive/
 │ ├── Project Alpha [abc12] - Full Metadata Archive.html
 │ ├── Project Alpha [abc12] - Full Metadata Archive.json
 │ ├── Project Alpha [abc12] - Full Metadata Archive Export Summary.json
 │ └── API Responses/
 │  ├── Project Alpha [abc12] - Core API Response.json
 │  ├── Project Alpha [abc12] - Contributors.json
 │  ├── Project Alpha [abc12] - Custom Metadata.json
 │  ├── Project Alpha [abc12] - CEDAR Metadata.json
 │  └── additional related metadata records
 │
 ├── Wikis/
 │ ├── Current/
 │ │ ├── Project Alpha [abc12] - Wiki - Home [wiki-id].md
 │ │ └── Project Alpha [abc12] - Current Wikis Export Summary.json
 │ └── Version History/
 │  ├── Home [wiki-id]/
 │  │ ├── individual wiki-version files
 │  │ ├── version-history index.csv
 │  │ └── version-history index.json
 │  └── Project Alpha [abc12] - Wiki Version History Export Summary.json
 │
 ├── Activity Logs/
 │ ├── Project Alpha [abc12] - Activity Log.csv
 │ ├── Project Alpha [abc12] - Activity Log.json
 │ └── Project Alpha [abc12] - Activity Logs Export Summary.json
 │
 └── Component One [def34]/
  ├── Full Metadata Archive/
  │ ├── Component One [def34] - Full Metadata Archive.html
  │ ├── Component One [def34] - Full Metadata Archive.json
  │ └── API Responses/
  ├── Wikis/
  │ ├── Current/
  │ └── Version History/
  └── Activity Logs/
   ├── Component One [def34] - Activity Log.csv
   └── Component One [def34] - Activity Log.json

The “Metadata Summary” folder appears only under the top-level project because its catalog and combined files already cover that project and all of its components. It is not repeated inside component folders.

The full metadata archive, wikis, and activity logs are stored separately inside each matching project or component folder. This preserves the OSF component hierarchy while keeping the different types of exported content distinct.

Each selected export type also produces its own export-summary JSON file under the top-level project. These files record the result of that export, including its completion status, counts, omissions, and errors.

Files and folders are named with both the OSF title and GUID so that projects and components remain identifiable if titles are duplicated or changed.


Choose another export location

Use “--output” to select a different folder.

Example for an external drive on macOS:


python3 osf_export_checklist_metadata_all_projects_v0_10.py \
 --output "/Volumes/External Drive/OSF Project Exports"


Example on Windows:


py .\osf_export_checklist_metadata_all_projects_v0_10.py `
 --output "D:\OSF Project Exports"

Use the same output location on future runs if you want the dashboard to restore your review control checkmarks.



Cancel, retry, and review warnings

Select “Cancel” on an export job to stop a queued or running export. Files that have already been written remain in their corresponding export folders. Because cancellation does not identify every unfinished element, restarting a cancelled job uses “Retry full action”.

The dashboard reports one of three completion outcomes:

- “Completed” — all requested content was exported successfully.
- “Completed with omissions” — the primary export completed, but one or more identified elements could not be downloaded.
- “Failed” — core metadata or the primary requested content could not be exported.

The exporter automatically retries temporary OSF errors, including rate limits and gateway errors, before reporting an omission or failure.

If a job finishes with omissions or fails:

1. Expand the issue section beneath the job.
2. Review the affected project or component, element type, element identifier, and reason for the problem.
3. Check the corresponding output folder. Successful files are retained even when other elements fail.
4. Use the retry option after correcting an access problem or after a temporary OSF service problem has passed.

Depending on the issue, the dashboard displays one of two retry options:

- “Retry affected items” - reruns only the failed or omitted content type for the identified projects or components. Successfully exported content is not downloaded again.
- “Retry full action” - repeats that export option for the complete project tree. This is used when the unfinished work cannot be isolated safely, including after cancellation or a failure involving core requested content.

Each selected export type is handled independently. For example, an omission in a wiki-version-history job does not require rerunning metadata summaries, full metadata archives, current wikis, or activity logs.

The issue section remains open while the dashboard refreshes, allowing its details to be reviewed while other jobs continue.


Stop the checklist

When all jobs have finished:

1. Return to Terminal or PowerShell.
2. Press ‘Control-C’.

Closing only the browser tab does not stop the local Python process.

