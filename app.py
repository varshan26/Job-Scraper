from flask import Flask, request, jsonify, render_template_string, Response
from curl_cffi import requests as curl_requests
import urllib.parse
import json
import re
import time
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

app = Flask(__name__)

# --- 1. HIRINGCAFE ATS SCRAPER LOGIC ---
PREFERRED_ATS_DOMAINS = [
    "myworkdayjobs.com", "oraclecloud.com", "taleo.net", "icims.com", 
    "successfactors", "zohorecruit", "smartrecruiters.com", "greenhouse.io", 
    "lever.co", "eightfold.ai", "workable.com", "jobvite.com", 
    "bamboohr.com", "ashbyhq.com", "phenompro.com"
]

def is_preferred_ats(url):
    if not url: return False
    return any(ats in url.lower() for ats in PREFERRED_ATS_DOMAINS)

# --- 2. YOUR JD EXTRACTOR CLASS ---
JS_RENDERED_PATTERNS = [
    'myworkdayjobs.com', 'workday.com', 'wd1.myworkdayjobs', 'wd3.myworkdayjobs',
    'wd5.myworkdayjobs', 'oraclecloud.com', 'taleo.net', 'fa-', '/fa/',
    'icims.com', 'icims'
]
SESSION_TIMEOUT_PHRASES = [
    'are you still with us', 'session will end', 'continue working',
    'session has expired', 'session timeout', 'click continue to proceed',
]

class JobDescriptionExtractor:
    def __init__(self, headless=True, nav_timeout_ms=30000):
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        }
        self.headless = headless
        self.nav_timeout_ms = nav_timeout_ms

    def extract(self, url):
        if self._needs_browser(url):
            html = self._render_with_browser(url)
            if not html: html = self._fetch_static(url)
        else:
            html = self._fetch_static(url)
            if html is None or self._looks_bot_blocked(html):
                browser_html = self._render_with_browser(url)
                if browser_html: html = browser_html

        if not html: return None

        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup.find_all(['script', 'style', 'noscript', 'template', 'nav', 'footer']):
            tag.decompose()

        strategies = [
            self._extract_workday, self._extract_greenhouse, self._extract_lever,
            self._extract_icims, self._extract_with_selectors, self._extract_meta_description,
            self._extract_content_area, self._extract_by_headings,
        ]

        for strategy in strategies:
            result = strategy(soup, url)
            if not result or len(result) <= 200 or self._looks_like_json(result): continue
            return self._clean_text(result)
        return None

    def _needs_browser(self, url):
        return any(p in url.lower() for p in JS_RENDERED_PATTERNS)

    def _looks_bot_blocked(self, html):
        if not html or len(html) < 800: return True
        block_signatures = ['access denied', 'request blocked', 'are you a human', "i'm not a robot", 'captcha', 'cf-browser-verification']
        return any(sig in html.lower() for sig in block_signatures)

    def _fetch_static(self, url):
        try:
            import requests
            response = requests.get(url, headers=self.headers, timeout=15)
            response.raise_for_status()
            return response.text
        except: return None

    def _render_with_browser(self, url):
        if not PLAYWRIGHT_AVAILABLE: return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(user_agent=self.headers['User-Agent'])
                page = context.new_page()
                page.goto(url, timeout=self.nav_timeout_ms, wait_until='domcontentloaded')

                if 'icims.com' in url:
                    try:
                        iframe_element = page.wait_for_selector('iframe#icims_content_iframe', timeout=5000)
                        if iframe_element:
                            frame = iframe_element.content_frame()
                            page.wait_for_timeout(2000)
                            html = frame.content()
                            browser.close()
                            return html
                    except PlaywrightTimeoutError: pass

                try: page.wait_for_load_state('networkidle', timeout=8000)
                except: pass

                body_text = page.inner_text('body').lower()
                if any(phrase in body_text for phrase in SESSION_TIMEOUT_PHRASES):
                    for label in ['Continue', 'OK', 'Ok', 'Yes', 'Resume']:
                        try:
                            page.get_by_role('button', name=label).click(timeout=2000)
                            page.wait_for_timeout(2000)
                            break
                        except: continue

                html = page.content()
                browser.close()
                return html
        except: return None

    def _extract_icims(self, soup, url):
        if 'icims' not in url.lower(): return None
        content = soup.find('div', class_='iCIMS_JobContent')
        return content.get_text(separator='\n', strip=True) if content else None

    def _extract_with_selectors(self, soup, url):
        selectors = ['.job-description', '.description', '#jobDescription', '.posting-requirements', '.jd-content']
        for selector in selectors:
            elements = soup.select(selector)
            if elements:
                text = elements[0].get_text(separator='\n', strip=True)
                if len(text) > 200: return text
        return None

    def _extract_workday(self, soup, url):
        if not self._needs_browser(url): return None
        content = soup.find(attrs={'data-automation-id': 'jobPostingDescription'})
        if content:
            text = content.get_text(separator='\n', strip=True)
            if len(text) > 200: return text
        return None

    def _extract_meta_description(self, soup, url): return None # Minimized for brevity, relies on other fallbacks safely
    
    def _looks_like_json(self, text):
        stripped = text.strip()
        if not stripped: return False
        return stripped[0] in '{[' or len(re.findall(r'"\w[\w\s-]*"\s*:', stripped[:2000])) > 5

    def _extract_greenhouse(self, soup, url):
        if 'greenhouse.io' not in url: return None
        content = soup.find('div', id='content') or soup.find('div', class_='content')
        if not content: return None
        return content.get_text(separator='\n', strip=True)

    def _extract_lever(self, soup, url):
        if 'lever.co' not in url: return None
        elements = soup.select('.posting, .posting-description')
        sections = [elem.get_text(separator='\n', strip=True) for elem in elements if len(elem.get_text(strip=True)) > 20]
        return '\n\n'.join(sections) if sections else None

    def _extract_content_area(self, soup, url):
        candidates = soup.find_all(['main', 'article']) + soup.find_all('div', class_=re.compile(r'content|main|job', re.I))
        best_text = ""
        for content in candidates:
            text = content.get_text(separator='\n', strip=True)
            if len(text) > len(best_text): best_text = text
        return best_text if len(best_text) > 300 else None

    def _extract_by_headings(self, soup, url): return None 

    def _clean_text(self, text):
        if not text: return None
        text = re.sub(r'\n\s*\n', '\n\n', text)
        text = re.sub(r' +', ' ', text)
        skip_patterns = [r'apply for this job', r'create a job alert', r'resume/cv', r'omb control number']
        lines = text.split('\n')
        filtered_lines = []
        for line in lines:
            if len(line) < 2: continue
            if len(line) <= 150 and any(re.search(p, line.lower()) for p in skip_patterns): continue
            filtered_lines.append(line)
        cleaned = '\n'.join(filtered_lines)
        return cleaned if len(cleaned) > 50 else None

# --- 3. FLASK ROUTES ---
@app.route('/')
def home():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/scrape', methods=['POST'])
def scrape():
    # Same logic as before to extract HiringCafe links
    data = request.json
    browser_url = data.get('url')
    if not browser_url: return jsonify({"error": "No URL provided"}), 400

    try:
        parsed_url = urllib.parse.urlparse(browser_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        if "searchState" not in query_params: return jsonify({"error": "No searchState found."}), 400
            
        search_state = json.loads(query_params["searchState"][0])
        search_state["dateFetchedPastNDays"] = 1 
        encoded_state = urllib.parse.quote(json.dumps(search_state))
        build_id = "1NsGWl8d9PWGglzBznrsv" 
        api_url = f"https://hiringcafe.com/_next/data/{build_id}/index.json?searchState={encoded_state}"

        headers = {
            "accept": "*/*", "referer": "https://hiringcafe.com/",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        }

        response = curl_requests.get(url=api_url, headers=headers, impersonate="chrome120")
        if response.status_code != 200: return jsonify({"error": "Failed to fetch data"}), 500

        page_props = response.json().get("pageProps", {})
        jobs = page_props.get("ssrHits", [])
        
        approved_jobs = []
        for job in jobs:
            apply_link = job.get("apply_url", "")
            if is_preferred_ats(apply_link):
                job_data = job.get("v5_processed_job_data", {})
                approved_jobs.append({
                    "company": job_data.get("company_name", "Unknown"),
                    "title": job_data.get("core_job_title", "Unknown"),
                    "link": apply_link
                })

        return jsonify({"total_found": len(jobs), "total_approved": len(approved_jobs), "jobs": approved_jobs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/download_jd', methods=['POST'])
def download_jd():
    data = request.json
    url = data.get('url')
    company = data.get('company', 'Unknown')
    
    if not url: return jsonify({"error": "No URL provided"}), 400

    extractor = JobDescriptionExtractor(headless=True)
    job_text = extractor.extract(url)

    if not job_text:
        return jsonify({"error": "Could not extract Job Description. Site may be protected."}), 404

    # Format the text output beautifully
    final_text = f"Company: {company}\nSource URL: {url}\n{'='*80}\n\n{job_text}"
    return jsonify({"text": final_text})


# --- 4. BRUTALIST HTML & CSS FRONTEND ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ATS JOB RIPPER</title>
    <style>
        * { box-sizing: border-box; }
        body { background: #dfdfdf; color: #000; font-family: 'Courier New', Courier, monospace; margin: 0; padding: 40px 20px; display: flex; flex-direction: column; align-items: center; }
        h1 { font-size: 3rem; text-transform: uppercase; background: #e5ff00; border: 4px solid #000; padding: 10px 20px; box-shadow: 8px 8px 0px #000; margin-bottom: 40px; text-align: center; }
        .container { width: 100%; max-width: 800px; background: #fff; border: 4px solid #000; padding: 30px; box-shadow: 12px 12px 0px #000; }
        label { font-weight: bold; font-size: 1.2rem; text-transform: uppercase; display: block; margin-bottom: 10px; }
        input[type="text"] { width: 100%; padding: 15px; font-size: 1rem; border: 3px solid #000; font-family: inherit; margin-bottom: 20px; outline: none; }
        button.rip-btn { width: 100%; padding: 15px; font-size: 1.5rem; font-weight: bold; background: #ff3366; color: #fff; border: 4px solid #000; box-shadow: 6px 6px 0px #000; cursor: pointer; text-transform: uppercase; transition: all 0.1s; font-family: inherit;}
        button.rip-btn:active { transform: translate(6px, 6px); box-shadow: 0px 0px 0px #000; }
        #status { margin-top: 20px; font-weight: bold; font-size: 1.2rem; }
        .job-card { border: 3px solid #000; background: #fff; margin-top: 15px; padding: 15px; box-shadow: 4px 4px 0px #000; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
        .job-info h3 { margin: 0 0 5px 0; font-size: 1.2rem; }
        .job-info p { margin: 0; font-size: 1rem; color: #444; font-weight: bold; }
        .btn-group { display: flex; gap: 10px; }
        .apply-btn { background: #000; color: #00ff00; padding: 10px; text-decoration: none; font-weight: bold; border: 2px solid #000; cursor: pointer;}
        .apply-btn:hover { background: #e5ff00; color: #000; }
        .jd-btn { background: #fff; color: #000; padding: 10px; font-weight: bold; border: 2px solid #000; cursor: pointer; font-family: inherit; }
        .jd-btn:hover { background: #000; color: #fff; }
    </style>
</head>
<body>
    <h1>ATS Job Ripper</h1>
    <div class="container">
        <label>Paste HiringCafe URL Here</label>
        <input type="text" id="urlInput" placeholder="https://hiringcafe.com/?searchState=...">
        <button class="rip-btn" onclick="fetchJobs()">Rip Jobs</button>
        <div id="status"></div>
        <div id="results"></div>
    </div>

    <script>
        async function fetchJobs() {
            const url = document.getElementById('urlInput').value;
            const status = document.getElementById('status');
            const results = document.getElementById('results');
            
            if(!url.includes('searchState')) {
                status.innerHTML = '<span style="color:red; background:#000; padding:5px;">ERROR: NEED SEARCHSTATE URL</span>'; return;
            }
            status.innerHTML = 'RIPPER INITIATED...';
            results.innerHTML = '';

            try {
                const response = await fetch('/api/scrape', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url: url })
                });
                const data = await response.json();
                if(!response.ok) throw new Error(data.error);

                status.innerHTML = `<span style="background:#e5ff00; padding:5px; border: 2px solid #000;">FOUND ${data.total_approved} ATS LINKS.</span>`;
                
                data.jobs.forEach((job, index) => {
                    results.innerHTML += `
                        <div class="job-card">
                            <div class="job-info">
                                <h3>${job.company}</h3>
                                <p>${job.title}</p>
                            </div>
                            <div class="btn-group">
                                <button class="jd-btn" id="jd-btn-${index}" onclick="downloadJD('${job.link}', '${job.company}', ${index})">⬇️ GET JD.TXT</button>
                                <a href="${job.link}" target="_blank" class="apply-btn">APPLY -></a>
                            </div>
                        </div>
                    `;
                });
            } catch (err) { status.innerHTML = `<span style="color:red; background:#000; padding:5px;">FAILED: ${err.message}</span>`; }
        }

        async function downloadJD(url, company, index) {
            const btn = document.getElementById(`jd-btn-${index}`);
            const originalText = btn.innerText;
            btn.innerText = "SCRAPING...";
            btn.disabled = true;

            try {
                const response = await fetch('/api/download_jd', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, 
                    body: JSON.stringify({ url: url, company: company })
                });
                const data = await response.json();
                if(!response.ok) throw new Error(data.error);

                // Create a downloadable text file in the browser
                const blob = new Blob([data.text], { type: 'text/plain' });
                const downloadUrl = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = downloadUrl;
                a.download = `${company.replace(/[^a-zA-Z0-9]/g, '_')}_JD.txt`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                
                btn.innerText = "✅ DOWNLOADED";
            } catch (err) {
                alert("Failed to extract JD: " + err.message);
                btn.innerText = "❌ FAILED";
            }
            setTimeout(() => { btn.disabled = false; }, 2000);
        }
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
