# download_dashboard

Download metadata, wikis, and activity logs of multiple projects/components (requires additional software)

You can download metadata, wikis, activity logs, and files as zip for your OSF Projects and Components through the OSF API using an available Python script, terminal/command-line commands, and a locally hosted dashboard. This may be a valuable asset if you are moving OSF content, preserving a snapshot of your work, or continuing a project in a separate repository.
This guide will help you download the following for projects and components that you can access:
1.  Project metadata - readable HTML and comprehensive JSON records containing the project or component title, contributors, description, license, identifiers, relationships, and other available metadata.
2.  Project wikis - the current version of every wiki page, every available historical version, or both, in Markdown format.
3.  Activity log data - including the date, action type, affected project or component, and other details supplied with each event. Activity logs are provided in CSV and JSON formats.
4.  Project files - one ZIP archive for each configured storage provider on each project or component.
The export preserves the project and component hierarchy. Folder and file names include both the OSF title and GUID.
Each type of content is selected separately. You do not have to download project files in order to export metadata, wikis, or activity logs.



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

The script is available on GitHub, named `osf_export_checklist_metadata_all_projects_v0_10.py` (temporary:https://github.com/eolson3/metadata_download_dashboard/blob/main/osf_export_checklist_metadata_all_projects_v0_15.py). Go to this page and click the “download raw file” image near the top right.





Step 4: Confirm that you have the current script

This guide applies to dashboard version 0.15.0.
On macOS, run the following to check:
cd ~/Downloads
python3 osf_export_checklist_metadata_all_projects_v0_15.py --version
 
Your Mac may ask you to confirm that Terminal has permission to access your Downloads folder. Select “Allow” to continue.
On Windows:
cd $HOME\Downloads
py .\osf_export_checklist_metadata_all_projects_v0_15.py --version
 
The result should include 0.15.0.
If it reports a different version, make sure you are running the newly downloaded file rather than one with a similar name. If the GitHub link or browser download still supplies an older file, delete the downloaded copy, reload the GitHub page, and use the “Download raw file” button again.



Step 5: Start the checklist

By default, the checklist writes exports to the “OSF Project Exports” folder within Downloads. You can choose another destination if you prefer. See Choose another export location.
On macOS, run the following:
cd ~/Downloads
python3 osf_export_checklist_metadata_all_projects_v0_15.py
 
On Windows, run the following:
cd $HOME\Downloads
py .\osf_export_checklist_metadata_all_projects_v0_15.py
 
The script will prompt you to paste your OSF token. The token will not appear on screen while you paste or type it. That is expected.
If you paste your token correctly, the script will confirm which account is logged in, retrieve all projects and components accessible to that account, reconstruct their hierarchy, and open the checklist in your browser. An account with many projects may take several minutes to load.
You must keep the Terminal or PowerShell window open while using the checklist. The dashboard is hosted only on your computer at 127.0.0.1. Your token remains in the running Python process and is not placed in the browser page or saved with the checklist.
If the browser does not open automatically, copy the local address printed in the terminal. It will resemble:
http://127.0.0.1:8765/


Step 6: Find and filter projects
The dashboard lists top-level projects and their accessible components. The counts at the top distinguish “Top-level projects” from the total number of “Projects and components”.
You can search by title, GUID, or category. You can also filter by:
1.  Visibility - public or private
2.  Access permission - the requesting user’s Admin, Write, or Read permission
3.  Review status - reviewed or unreviewed (See Step 7: Track completed projects)
The access-permission column and filter refer only to the person whose token is being used. They do not list or combine the permissions of other contributors.
A top-level project tree remains visible when the top-level project or one of its components matches the current filters. Export actions always operate on the complete accessible project tree, not on an individual component selected from the display.
Use “Expand all” or “Collapse all” to show or hide component lists.


Step 7: Download information for one project
Each top-level project has five export buttons:
1.  Comprehensive metadata - exports a readable HTML record and a comprehensive JSON record for the project and every accessible component beneath it.
2.  Current wikis - exports the most recent content of every wiki page.
3.  Wiki history - exports every available historical wiki version, together with CSV and JSON version indexes.
4.  Activity logs - exports activity records in CSV and JSON and organizes them into the matching project/component tree.
5.  Download files as ZIP - downloads one ZIP archive for each configured storage provider on the project and every accessible component beneath it.
Current wikis and wiki history are separate choices. Select both if you want an easily identifiable current copy and the complete available version history.
Exports run in the background, one job at a time. You can queue other exports while a job is running, but the terminal window must remain open until they finish. Each export type writes to its own named folder, so metadata, wikis, activity logs, and file ZIPs can be downloaded independently into the same project tree.
File ZIPs may be much larger and take much longer than the other exports. A project or component with multiple configured storage providers receives a separate ZIP for each provider.


Step 8: Track reviewed top-level projects
The checkbox to the left of each top-level project is a review control. Checking it marks the top-level project and all of its current components as reviewed. Components do not have separate review checkboxes because exports are controlled from the top-level project.
Review checkmarks are saved locally and restored when you run the checklist again, provided that you use:
1.  The same OSF account
2.  The same export folder
The saved state is stored in a hidden file inside the export folder:
.checklist-state-[OSF account ID].json
 
The checklist does not automatically mark a project as reviewed after an export. You control the review checkmarks.
The “Include in batch” selections and selected export types are temporary and are not restored the next time the dashboard runs.
Use “Clear review checks” only when you want to remove every saved review checkmark.
Use “Export checklist CSV” to create an offline checklist. The CSV includes:
3.  GUID
4.  Title
5.  URL
6.  Visibility
7.  Access permission for the requesting user
8.  Depth in the project/component tree
9.  Parent GUID
10.        Reviewed status



Step 9: Export several top-level projects
To create a batch:
1.  Select “Include in batch” on each desired top-level project, or use “Select all visible projects”.
2.  Check one or more content types in the Batch export section:
○   Comprehensive metadata
○   Current wikis
○   Wiki version history
○   Activity logs
○   Files as ZIP
3.  Select “Run selected exports”.
“Select all visible projects” selects only the top-level projects that are visible after the current search, visibility, access-permission, and review-status filters have been applied. It does not select filtered-out projects. The button displays the number of visible top-level projects it will select.
Each selected content type for each selected top-level project becomes a separate queued job. Every job includes the accessible components beneath that top-level project. Jobs run sequentially, with only one export running at a time.
You do not need to keep the browser tab in the foreground, but the terminal must remain active. Large batches, wiki histories, and file ZIPs may take considerable time. If you are exporting a large account, it may be easier to work in smaller groups and use the review control to track completed project trees.
What you can export
Comprehensive metadata export
If the comprehensive metadata export is chosen, each project or component has its own “Metadata” folder:
Metadata/
├── Project Title [abc12] - Complete Metadata.html
└── Project Title [abc12] - Complete Metadata.json
 
The comprehensive metadata export follows supported linked OSF API relationships instead of saving only the basic project response. When available, it includes:
4.  Title, GUID, URL, category, description, tags, subjects, visibility, and dates
5.  Contributors, bibliographic status, permissions, profiles, and identifiers
6.  Affiliated institutions
7.  OSF, DOI, and other identifiers
8.  License and rights information
9.  Language and resource type
10.        Funders, awards, and grants
11.        Registrations, preprints, forks, and linked OSF resources
12.        Storage region and configured storage providers
13.        Custom metadata and CEDAR metadata records
14.        Parent and component relationships
Storage-provider records in the metadata document describe configured providers. Project files are downloaded only when “Files as ZIP” is selected.
The HTML is intended for people to read. The matching JSON retains the assembled metadata package, including the core project record, custom and CEDAR metadata, resolved relationship records, and a relationship catalog. Separate copies of every underlying API response are not created.
Wiki content and activity logs are handled by their own export options. View-only links are not retrieved because they can provide access to private content.
Current wiki export
For every project and component, the current wiki export creates one Markdown file containing the most recent content of each available wiki page.
Example:
Wikis/
└── Current/
	└── Project Title [abc12] - Wiki - Home [wiki-id].md
 
Wiki version-history export
For every project and component, the wiki version-history export creates a folder for each wiki page containing:
15.        Every available historical version as an individual Markdown file
16.        A CSV version-history index
17.        A JSON version-history index
Example:
Wikis/
└── Version History/
	└── Home [wiki-id]/
    	├── Project Title [abc12] - Wiki - Home [wiki-id] - Version 0001 - date.md
    	├── Project Title [abc12] - Wiki - Home [wiki-id] - Version 0002 - date.md
    	├── Project Title [abc12] - Wiki - Home [wiki-id] - Version History.csv
    	└── Project Title [abc12] - Wiki - Home [wiki-id] - Version History.json
 
Wiki exports can take time when projects contain many pages or long version histories.
Activity-log export
Activity logs are written in two formats:
18.        CSV for spreadsheets and review
19.        JSON for preserving the complete returned activity records
The script assigns activity entries to the project or component where the activity originated when that information is available.
Logs associated with a deleted, former, or inaccessible component may appear beneath:
Activity Logs/
└── Former or inaccessible components/
 
This preserves activity that might otherwise be difficult to associate with the current project tree.
Files as ZIP export
The file export asks OSF for a ZIP archive for each configured storage provider on every project and component in the selected tree.
Example:
Files/
├── Project Title [abc12] - Files - OSF Storage.zip
└── Project Title [abc12] - Files - Box.zip
 
The ZIPs are stored in the “Files” folder belonging to the matching project or component. This preserves the OSF project/component hierarchy while keeping different storage providers separate.
The script streams each download to a temporary file, verifies that OSF returned a valid ZIP, and then moves the completed ZIP into place. If a provider does not supply a ZIP-capable link or a download repeatedly fails, the dashboard reports that provider as an omission and retains other successful exports.
Export folder structure
By default, exports are written to:
Downloads/OSF Project Exports
Only folders corresponding to selected export options are created. If all export options are selected, a completed export may resemble:
OSF Project Exports/
└── Project Alpha [abc12]/
	├── Metadata/
	│   ├── Project Alpha [abc12] - Complete Metadata.html
	│   ├── Project Alpha [abc12] - Complete Metadata.json
	│   └── Project Alpha [abc12] - Comprehensive Metadata Export Summary.json
	├── Wikis/
	│   ├── Current/
	│   │   ├── Project Alpha [abc12] - Wiki - Home [wiki-id].md
	│   │   └── Project Alpha [abc12] - Current Wikis Export Summary.json
	│   └── Version History/
	│   	├── Home [wiki-id]/
	│   	│   ├── individual wiki-version files
	│   	│   ├── version-history index.csv
	│   	│   └── version-history index.json
	│   	└── Project Alpha [abc12] - Wiki Version History Export Summary.json
	├── Activity Logs/
	│   ├── Project Alpha [abc12] - Activity Log.csv
	│   ├── Project Alpha [abc12] - Activity Log.json
	│   └── Project Alpha [abc12] - Activity Logs Export Summary.json
	├── Files/
	│   ├── Project Alpha [abc12] - Files - OSF Storage.zip
	│   ├── Project Alpha [abc12] - Files - Box.zip
	│   └── Project Alpha [abc12] - Files as ZIP Export Summary.json
	└── Component One [def34]/
    	├── Metadata/
        │   ├── Component One [def34] - Complete Metadata.html
        │   └── Component One [def34] - Complete Metadata.json
    	├── Wikis/
        │   ├── Current/
        │   └── Version History/
    	├── Activity Logs/
        │   ├── Component One [def34] - Activity Log.csv
        │   └── Component One [def34] - Activity Log.json
    	└── Files/
            └── Component One [def34] - Files - OSF Storage.zip
 
The metadata, wikis, activity logs, and files are stored separately inside each matching project or component folder. This preserves the OSF component hierarchy while keeping the different types of exported content distinct.
Each selected export type produces its own export-summary JSON file under the corresponding top-level project folder. These summaries record completion status, counts, omissions, failures, and retry information.
Files and folders are named with both the OSF title and GUID so that projects and components remain identifiable if titles are duplicated or changed.


Choose another export location
Use --output to select a different folder.
Example for an external drive on macOS:
python3 osf_export_checklist_metadata_all_projects_v0_15.py \
  --output "/Volumes/External Drive/OSF Project Exports"
 
Example on Windows:
py .\osf_export_checklist_metadata_all_projects_v0_15.py `
  --output "D:\OSF Project Exports"
 
Use the same output location on future runs if you want the dashboard to restore your review-control checkmarks.


Cancel, retry, and review warnings
Select “Cancel” on an export job to stop a queued or running export. Files that have already been written remain in their corresponding export folders. Because cancellation does not identify every unfinished element, restarting a cancelled job uses “Retry full action”.
The dashboard reports one of three completion outcomes:
1.  “Completed” - all requested content was exported successfully.
2.  “Completed with omissions” - the primary export completed, but one or more identified elements could not be downloaded.
3.  “Failed” - core metadata or the primary requested content could not be exported. Retry is recommended.
The exporter automatically retries temporary OSF errors, including rate limits and gateway errors, before reporting an omission or failure. Repeated warnings do not necessarily mean that the entire export failed. The final outcome and issue details show what succeeded and what did not.
If a job finishes with omissions or fails:
4.  Expand the issue section beneath the job.
5.  Review the affected project or component, element type, element identifier, and reason for the problem.
6.  Check the corresponding output folder. Successful files are retained even when other elements fail.
7.  Use the retry option after correcting an access problem or after a temporary OSF service problem has passed.
Depending on the issue, the dashboard displays one of two retry options:
8.  “Retry affected items” - reruns only the failed or omitted content type for the identified projects or components. Successfully exported content is not downloaded again.
9.  “Retry full action” - repeats that export option for the complete project tree. This is used when the unfinished work cannot be isolated safely, including after cancellation or a failure involving core requested content.
Each selected export type is handled independently. For example, an omission in a wiki-version-history job does not require rerunning comprehensive metadata, current wikis, activity logs, or file ZIPs.
The issue section remains open while the dashboard refreshes.
Refresh the checklist after changing an OSF project
The checklist retrieves the current OSF inventory each time the command starts. Stop and rerun the command to recognize changes to titles, visibility, permissions, components, and other project information.
Review checks are restored from the local state file. If a reviewed top-level project has gained a new accessible component, that component is included in the reviewed project tree when the checklist is rebuilt.
Existing exported files are not automatically deleted, renamed, or compared with OSF. Re-run the desired export actions to create a new snapshot. If an OSF title has changed, the new title and GUID may produce a new folder while an older folder remains in the export location.


Troubleshoot token and connection errors
If the script reports HTTP 401 or says that OSF rejected the personal access token:
1.  Create a new token in OSF with the osf.full_read scope.
2.  Copy the token value, not the token name.
3.  Run the script again and paste the new token without quotation marks or extra spaces.
4.  Confirm that the token was created on the same OSF environment that the script is using.
An invalid token is not corrected by repeatedly retrying the same request. Generate and use a current token.
If the script reports that it cannot reach OSF after several attempts, the cause may be a local internet connection, VPN, proxy, firewall, security product, DNS service, or institutional network. This can affect one computer even when the script works for other users.
If the script reports an SSL certificate verification error:
1.  On macOS with Python from python.org, open the Python folder in Applications and run Install Certificates.command.
2.  Confirm that the computer’s date and time are correct.
3.  Temporarily test without a VPN if your organization permits it.
4.  Ask local technical support whether a proxy or security product requires an organizational certificate to be installed for Python.
5.  Update or reinstall Python from python.org if the certificate installation is damaged or very old.
You can test whether Python can reach the public OSF API without providing a token.
On macOS:
python3 -c "import urllib.request; print(urllib.request.urlopen('https://api.osf.io/v2/', timeout=30).status)"
 
On Windows:
py -c "import urllib.request; print(urllib.request.urlopen('https://api.osf.io/v2/', timeout=30).status)"
 
If this test also fails, the problem is with Python’s network connection or certificate setup rather than the dashboard token.



Stop the checklist

When all jobs have finished:

1. Return to Terminal or PowerShell.
2. Press ‘Control-C’.

Closing only the browser tab does not stop the local Python process.


cd ~/Downloads
python3 osf_project_download_links_v1_1.py --limit 5
cd ~/Downloads
python3 -c "import urllib.request; print(urllib.request.urlopen('https://api.osf.io/v2/', timeout=30).status)"
