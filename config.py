import os

import playhouse.db_url

# Environment shortcut and path helpers

root = os.path.dirname(os.path.abspath(__file__))
rel = lambda base, *p: os.path.join(base, *p)

env = os.environ

# Tornado web application settings
# http://www.tornadoweb.org/en/stable/web.html#tornado.web.Application.settings
#
# For production deployments you should provide a fresh cookie secret.
# You can generate a random hex string on the command-line like this:
# python -c "import random; print '%0256x' % random.randrange(16**256)"

settings = dict(
    cookie_secret       = env["COOKIE_SECRET"],         # required
    debug               = env.get("DEBUG", "0") == "1", # set for development
    login_url           = "/auth",
    static_path         = rel(root, "static"),
    static_url_prefix   = "/static/",
    template_path       = rel(root, "templates"),
    xheaders            = True,
    xsrf_cookies        = True,
)

# Database configuration (PostgreSQL)
# http://peewee.readthedocs.org/en/latest/peewee/database.html#using-postgresql
#
# The connection parameters are parsed from a DSN (Data Source Name) string.
# A Postgres DSN string typically uses the following scheme:
# DATABASE_URL="postgres://user:password@host/database"

dbconf = playhouse.db_url.parse(env["DATABASE_URL"])

# Blob store configuration

bsconf = dict(nodes=[], opts={})
