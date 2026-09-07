# wx-ecmwf

Image repo for the ECMWF model on WxModels. Same renderer as the main repo,
with `WX_MODEL: ecmwf` set in the workflow. Renders the 00Z and 12Z runs from
ECMWF open data (0.25°, 0–240 h) to this repo's GitHub Pages site; the main
site lists this site's URL in `site/config.js`.

Setup: create the repo, upload `render/`, `site/`, `requirements.txt`,
`.gitignore`; create `.github/workflows/render.yml` from the file here;
Settings → Pages → Source: GitHub Actions; run the workflow.
