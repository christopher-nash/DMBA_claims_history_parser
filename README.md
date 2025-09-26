# Convert DMBA Claims History PDF to CSV

https://www.dmba.com/sc/medical/HealthClaims.aspx?type=medical 
* Choose Prescription, Medical, Year-To-Date, and Detail report type.

![Medical Claims](/docs/medical-claims.png)
![Claims History Report Builder](/docs/claims_history_report_builder.png)

## Python Script

🐍 https://www.python.org/downloads/

Library dependencies:
* [pdfplumber](https://pypi.org/project/pdfplumber/).

Put your exported PDF report (PrintFriendlyEOB.pdf) into the same directory as the script then let it rip.

Usage:

```sh
pip install pdfplumber
python extract_claims_to_csv.py PrintFriendlyEOB.pdf history.csv
```
ℹ️ Note, you may need to use `pip3` and `python3` depending on your machine's setup

## Node JS Script

🌐 https://github.com/nvm-sh/nvm

Library dependencies:
* [pdfjs-dist](https://www.npmjs.com/package/pdfjs-dist) by Mozilla 

```sh
npm init -y
npm i pdfjs-dist
node extract_claims_to_csv.mjs PrintFriendlyEOB.pdf history.csv
```