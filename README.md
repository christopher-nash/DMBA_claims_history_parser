# Convert DMBA Claims History PDF to CSV

https://www.dmba.com/sc/medical/HealthClaims.aspx?type=medical 
* Choose Prescription, Medical, Year-To-Date, and Detail report type.

## Python Script

It only uses one Python library pdfplumber. https://pypi.org/project/pdfplumber/

Put your exported PDF report (PrintFriendlyEOB.pdf) into the same directory as the script then let it rip.
Usage:

```sh
pip install pdfplumber
python extract_claims_to_csv.py PrintFriendlyEOB.pdf history.csv
```
(Note, you may need to use `pip3` and `python3` depending on your machine's setup)

## NodeJS Script

It uses pdfjs-dist by Mozilla https://www.npmjs.com/package/pdfjs-dist

```sh
npm init -y
npm i pdfjs-dist
node extract_claims_to_csv.mjs PrintFriendlyEOB.pdf history.csv
```