from flask import Flask, request, jsonify, render_template_string
from curl_cffi import requests
import urllib.parse
import json

app = Flask(__name__)

PREFERRED_ATS_DOMAINS = [
    "myworkdayjobs.com", "oraclecloud.com", "taleo.net", "icims.com",
    "successfactors", "zohorecruit", "smartrecruiters.com", "greenhouse.io",
    "lever.co", "eightfold.ai", "workable.com", "jobvite.com",
    "bamboohr.com", "ashbyhq.com", "phenompro.com"
]

def is_preferred_ats(url):
    if not url: return False
    url_lower = url.lower()
    return any(ats in url_lower for ats in PREFERRED_ATS_DOMAINS)

@app.route('/')
def home():
    # We will serve the Brutalist HTML from the same file for simplicity
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/scrape', methods=['POST'])
def scrape():
    data = request.json
    browser_url = data.get('url')

    if not browser_url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        parsed_url = urllib.parse.urlparse(browser_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)

        if "searchState" not in query_params:
            return jsonify({"error": "No filters found in URL. Apply filters on HiringCafe first."}), 400

        search_state = json.loads(query_params["searchState"][0])
        search_state["dateFetchedPastNDays"] = 1

        encoded_state = urllib.parse.quote(json.dumps(search_state))
        build_id = "1NsGWl8d9PWGglzBznrsv" # Update this if HiringCafe updates their site
        api_url = f"https://hiringcafe.com/_next/data/{build_id}/index.json?searchState={encoded_state}"

        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": "https://hiringcafe.com/",
            "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
            "sec-ch-ua-platform": '"macOS"',
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        }

        response = requests.get(url=api_url, headers=headers, impersonate="chrome120")

        if response.status_code != 200:
            return jsonify({"error": f"Failed to fetch data: {response.status_code}"}), 500

        page_props = response.json().get("pageProps", {})
        jobs = page_props.get("ssrHits", [])

        approved_jobs = []
        for job in jobs:
            apply_link = job.get("apply_url", "")
            if is_preferred_ats(apply_link):
                job_data = job.get("v5_processed_job_data", {})
                approved_jobs.append({
                    "company": job_data.get("company_name", "Unknown Company"),
                    "title": job_data.get("core_job_title", "Unknown Title"),
                    "link": apply_link
                })

        return jsonify({
            "total_found": len(jobs),
            "total_approved": len(approved_jobs),
            "jobs": approved_jobs
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- BRUTALIST HTML & CSS FRONTEND ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATS JOB RIPPER</title>
    <style>
        * { box-sizing: border-box; }
        body {
            background-color: #dfdfdf;
            color: #000;
            font-family: 'Courier New', Courier, monospace;
            margin: 0; padding: 40px 20px;
            display: flex; flex-direction: column; align-items: center;
        }
        h1 {
            font-size: 3rem; text-transform: uppercase;
            background: #e5ff00; border: 4px solid #000;
            padding: 10px 20px; box-shadow: 8px 8px 0px #000;
            margin-bottom: 40px; text-align: center;
        }
        .container {
            width: 100%; max-width: 800px;
            background: #fff; border: 4px solid #000;
            padding: 30px; box-shadow: 12px 12px 0px #000;
        }
        label { font-weight: bold; font-size: 1.2rem; text-transform: uppercase; display: block; margin-bottom: 10px; }
        input[type="text"] {
            width: 100%; padding: 15px; font-size: 1rem;
            border: 3px solid #000; font-family: inherit; margin-bottom: 20px;
            box-shadow: inset 4px 4px 0px rgba(0,0,0,0.1); outline: none;
        }
        input[type="text"]:focus { background: #f0f8ff; }
        button {
            width: 100%; padding: 15px; font-size: 1.5rem; font-weight: bold;
            background: #ff3366; color: #fff; border: 4px solid #000;
            box-shadow: 6px 6px 0px #000; cursor: pointer;
            text-transform: uppercase; font-family: inherit; transition: all 0.1s;
        }
        button:active { transform: translate(6px, 6px); box-shadow: 0px 0px 0px #000; }

        #status { margin-top: 20px; font-weight: bold; font-size: 1.2rem; }

        #results { margin-top: 30px; }
        .job-card {
            border: 3px solid #000; background: #fff; margin-bottom: 15px;
            padding: 15px; box-shadow: 4px 4px 0px #000; display: flex;
            justify-content: space-between; align-items: center; gap: 10px;
        }
        .job-info h3 { margin: 0 0 5px 0; font-size: 1.2rem; }
        .job-info p { margin: 0; font-size: 1rem; color: #444; font-weight: bold; }
        .apply-btn {
            background: #000; color: #00ff00; padding: 10px 20px;
            text-decoration: none; font-weight: bold; text-transform: uppercase;
            border: 2px solid #000; transition: background 0.2s;
        }
        .apply-btn:hover { background: #e5ff00; color: #000; }
    </style>
</head>
<body>

    <h1>ATS Job Ripper</h1>

    <div class="container">
        <label>Paste HiringCafe URL Here</label>
        <input type="text" id="urlInput" placeholder="https://hiringcafe.com/?searchState=...">
        <button onclick="fetchJobs()">Rip Jobs</button>

        <div id="status"></div>
        <div id="results"></div>
    </div>

    <script>
        async function fetchJobs() {
            const url = document.getElementById('urlInput').value;
            const status = document.getElementById('status');
            const results = document.getElementById('results');

            if(!url.includes('searchState')) {
                status.innerHTML = '<span style="color:red; background:#000; padding:5px;">ERROR: URL MUST CONTAIN FILTERS (searchState)</span>';
                return;
            }

            status.innerHTML = 'RIPPER INITIATED... BYPASSING CLOUDFLARE...';
            results.innerHTML = '';

            try {
                const response = await fetch('/api/scrape', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });

                const data = await response.json();

                if(!response.ok) throw new Error(data.error);

                status.innerHTML = `<span style="background:#e5ff00; padding:5px; border: 2px solid #000;">DONE! FILTERED OUT ${data.total_found - data.total_approved} JUNK SITES. FOUND ${data.total_approved} CLEAN ATS LINKS.</span>`;

                data.jobs.forEach(job => {
                    results.innerHTML += `
                        <div class="job-card">
                            <div class="job-info">
                                <h3>${job.company}</h3>
                                <p>${job.title}</p>
                            </div>
                            <a href="${job.link}" target="_blank" class="apply-btn">APPLY NOW -></a>
                        </div>
                    `;
                });
            } catch (err) {
                status.innerHTML = `<span style="color:red; background:#000; padding:5px;">FAILED: ${err.message}</span>`;
            }
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
