"""Guards BOILERPLATE_XPATH: it must eat site chrome and nothing else.

Regression: a bare contains(@class,"header") matched the theme's `page-header`
body block, so every legacy_project page lost its intro prose and Donate link
and kept only the donor-name roster.  Run: python test_extract.py
"""
from app import extract, ExtractIn

PAGE = """<html><body>
  <div class="header-box"><ul class="header-list"><li>About</li><li>Give</li></ul></div>
  <aside class="mobile-menu">Search for: About Overview Careers Alumni Corner</aside>
  <section class="comm-section page-header-section">
    <div class="page-header"><div class="detail-page-header-left-content">
      <h1>Class of 1995</h1>
      <p>The Class of 1995 batch celebrated their Silver Jubilee in 2020 and have supported
         a diverse range of initiatives, including Arena 95 and the vaccination drive.</p>
      <a href="https://portal.acr.iitbombay.org/payments/?funds=NTI=">Donate Now</a>
    </div></div>
  </section>
  <div class="section-header"><h2>Arena 95 - Basketball Court</h2>
    <p>IIT Bombay renamed its basketball court to honour the batch's generosity to campus sport.</p></div>
  <footer class="footer">Copyright ACR</footer>
</body></html>"""

md = extract(ExtractIn(html=PAGE))["markdown"]

assert "Silver Jubilee" in md, "page-header intro prose was pruned as chrome"
assert "basketball court" in md, "section-header project block was pruned as chrome"
assert "Alumni Corner" not in md, "mobile-menu chrome leaked into content"
assert "Copyright ACR" not in md, "footer chrome leaked into content"

print("ok")
