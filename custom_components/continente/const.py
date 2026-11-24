"""Constantes da integração Continente."""

DOMAIN = "continente"

BASE_URL = "https://mcp.continente.pt"
MCP_URL = f"{BASE_URL}/mcp"
AUTHORIZE_URL = f"{BASE_URL}/oauth/authorize"
TOKEN_URL = f"{BASE_URL}/oauth/token"
REGISTER_URL = f"{BASE_URL}/oauth/register"

# Sem order:create de propósito: a integração nunca encomenda nem paga.
SCOPES = "catalog:read basket:read basket:write order:read profile:read"

CONF_CLIENT_ID = "client_id"
CONF_TODO_ENTITY = "todo_entity"
DEFAULT_TODO_ENTITY = "todo.lista_de_compras"

# Quanto tempo guardar o produto encontrado para um nome antes de voltar a pesquisar.
LOOKUP_TTL = 6 * 3600
# Os "mais comprados" e favoritos mudam pouco.
USUALS_TTL = 12 * 3600
# Espera depois de a lista mudar, para juntar várias alterações numa só ronda.
LIST_DEBOUNCE = 5

STORAGE_VERSION = 1
