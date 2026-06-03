# TDD Agent — D365

Automates D365 Technical Design Document (TDD) generation by directly analyzing Azure DevOps TFVC Changesets. It uses Google Gemini AI to compare object versions and extract meaningful change summaries.

## ✨ Features

- **Changeset Automation**: Enter a Changeset ID to automatically fetch all changed objects.
- **Auto-Detection**: Automatically identifies object types (Table, Class, Form, EDT, etc.) from file paths.
- **Smart Comparison**: Uses AI to detect added, modified, or deleted fields, methods, and controls.
- **Live Preview**: Real-time TDD simulation in the browser.
- **Word Export**: Generates a professional `.docx` document styled with Hitachi branding.

## 🛠 Setup

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd tdd-agent
   ```

2. **Install dependencies**:
   ```bash
   pip install flask python-docx openai python-dotenv requests
   ```

3. **Configure Environment**:
   Create a `.env` file in the root directory (never commit this to Git!):
   ```env
   KIMI_API_KEY=your_kimi_api_key
   AZURE_DEVOPS_ORG=your_org_name
   AZURE_DEVOPS_PROJECT=your_project_name
   AZURE_DEVOPS_PAT=your_personal_access_token
   ```

4. **Run the application**:
   ```bash
   python app.py
   ```

## 🚀 Deployment

GitHub hosts **code**, but to let others use the application online, you need a **hosting provider**. Popular choices include:

- **Render / Heroku**: Great for quick Python/Flask deployments.
- **Azure App Service**: Native integration with Azure DevOps.
- **PythonAnywhere**: Simple and specialized for Flask.

### Deployment Security Note
- Use **Environment Variables** in your hosting provider's dashboard to store your API keys and PATs. **Never** hardcode them in your source code.

## ⚠️ Important Security Warning
**DO NOT UPLOAD YOUR `.ENV` FILE TO GITHUB.** 
The Personal Access Token (PAT) gives access to your source code. If you accidentally push it to a public repository, **revoke it immediately** in Azure DevOps and generate a new one.
