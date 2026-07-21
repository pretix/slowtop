slowtop - Browse and Analyse Slow PostgreSQL Queries
======================================================

Features:
* Find slow queries in log files or pg_stat_statements
* Show overview over all queries with their duration
* Show queries scrollable, pretty printed with syntax highlighting
* Show queries grouped by query_id with Statistical Data
* Send selected queries plan to [explain.dalibo.com](https://explain.dalibo.com) or an compatible site
* Run queries to get detailed execution plan
* Copy queries to Clipboard (even works over ssh)
* Extendable with your custom fields
* All the features Textual brings out of the box: keybindings, screenshots, etc.

Dependencies:
* PostgreSQL with logs in JSON-Format and/or pg_stat_statements enabled
* textual
* psycopg
* pglast
* requests
