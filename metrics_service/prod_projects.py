"""
prod_projects.py — Static production resource mapping for the daily report.

PROD_PROJECTS: the three production Azure resource groups with their
               App Services, MySQL, PostgreSQL, Redis, and Service Bus resources.
               UAT resource groups are intentionally excluded.
"""

PROD_PROJECTS = [
    {
        "name": "WeMasterTrade",
        "resource_group": "PROD_WE_MASTER_TRADE_SA",
        "app_services": [
            "wmt-frontend-prod-sa",
            "wmt-backoffice-prod-sa",
        ],
        "mysql": [
            "wmt-mysql-prod-sa",
        ],
        "postgres": [],
        "redis": [
            "wmt-rediscache-prod-sa",
        ],
        "service_bus": [
            "wmt-servicebus-prod-sa",
        ],
    },
    {
        "name": "WeGolden",
        "resource_group": "WGD_PROD",
        "app_services": [],
        "mysql": [],
        "postgres": [
            "lfg-wp-postgresql-prod",
        ],
        "redis": [
            "lfg-wp-rediscache-prod",
        ],
        "service_bus": [
            "wp-servicebus-prod",
        ],
    },
    {
        "name": "WeCopyTrade",
        "resource_group": "PROD_WE_COPY_TRADE",
        "app_services": [
            "wmt-app-bo-trading-mgt-prod-ca",
            "wct-frontend-prod",
            "wct-backoffice-prod",
        ],
        "mysql": [
            "wct-backoffice-mysql-prod",
        ],
        "postgres": [
            "dataplatform-psql-prod",
        ],
        "redis": [
            "dataplatform-rediscache-prod",
        ],
        "service_bus": [
            "wct-servicebus-prod",
        ],
    },
]
