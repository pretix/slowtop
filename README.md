slowtop - Browse and Analyse Slow PostgreSQL Queries
======================================================

Features:
* Read ether slow queries form log files or pg_stat_statements
* Show overview over all queries with there duration
* Show query scrollable, pretty printed with syntax highlighting
* Show queries grouped by query_id with Statistical Data
* Send query plan to [explain.dalibo.com](https://explain.dalibo.com) or an compatible site
* Run query to get detailed execution plan
* Copy query to Clipboard (even works over ssh)
* Extendable with your custom fields
* Can take screenshots

Dependencies:
* PostgreSQL with logs in JSON-Format and/or pg_stat_statements enabled
* textual
* psycopg
* sqlparse
* requests
