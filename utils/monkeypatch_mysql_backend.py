import django.db.backends.mysql.base

def fulltext_search_sql(self, field_name):
    return 'MATCH (%s) AGAINST (%%s IN NATURAL LANGUAGE MODE)' % field_name

def monkeypatch_mysql_backend():
    DatabaseOperations = django.db.backends.mysql.base.DatabaseOperations
    DatabaseOperations.fulltext_search_sql = fulltext_search_sql
