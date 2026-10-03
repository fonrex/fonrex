# Provider page fixtures

Each `<provider>_<ticker>.html` file is an extract of a **real page**, captured on the date
recorded in the matching `.json` file. The extract was reduced automatically to the smallest
document for which the provider's parser returns exactly the same result as on the full page,
plus the markup of the values the parser should read but currently does not.

The `.json` file holds:

- `expected`: what the parser returns today and what the page really displays — asserted as is;
- `known_gaps`: values displayed on the page that the parser misses or gets wrong. Each one is a
  strict `xfail` in `tests/test_provider_real_pages.py`: fixing the parser makes the test
  "unexpectedly pass", which is the signal to move the field to `expected`.

Only short extracts are stored (labels and figures), never a full page.

`search/` holds real search API responses used to test result selection.

To refresh a fixture after a site changes its markup, capture the page again, reduce it the same
way and update the `.json` file with the values displayed on the page that day.
