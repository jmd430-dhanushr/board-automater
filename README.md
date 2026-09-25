# Board Automater

Board Automater saves you time on the repetitive work of moving tasks and tickets from one board to another, for example from Jira to Azure DevOps.

It comes with a simple UI, so it's easy to use. Give it a try.

![Board Automater connection screen](docs/screenshot.png)

![Board Automater run sync screen](docs/run-sync.png)

## Run it

Clone the repo and go into its folder:

```sh
git clone <repo-url>
cd Board-Automater
```

### Option 1: Docker (recommended)

```sh
docker compose up --build
```

### Option 2: Without Docker

If you don't have Docker, you'll need [Python 3.11+](https://www.python.org/downloads/) and [Node.js 22+](https://nodejs.org/).

```sh
cd frontend
npm ci
npm run build
cd ..
pip install -r requirements.txt
uvicorn app.main:app
```

Either way, open http://localhost:8000, enter your Jira and Azure DevOps credentials, and start syncing.

> **Security:** Your credentials are never stored on the server. They stay in your browser's local storage and are only sent with each request to talk to Jira and Azure DevOps.

> **Note:** Always test your connection, run a **Preview**, and check the results before you run a real sync. A sync creates and updates work items on your board, so make sure you're confident in what it will do first. This tool is still maturing and is provided as is, and you're responsible for validating any changes it makes to your boards.
