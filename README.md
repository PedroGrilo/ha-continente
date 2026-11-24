# Continente para Home Assistant

Integração não oficial que liga o Home Assistant ao [Continente Online](https://www.continente.pt)
através do servidor MCP oficial do Continente (`mcp.continente.pt`).

- **Preços da lista de compras:** cada artigo por comprar de uma lista `todo` (por omissão
  `todo.lista_de_compras`) é ligado a um produto do Continente, com preço e alternativas.
- **Enviar para o carrinho:** põe os produtos no carrinho do Continente — o mesmo da app e do site.
- **Nunca encomenda nem paga:** o login não pede a permissão `order:create`. Revês e pagas na app.

## Instalação (HACS)

1. HACS → ⋮ → Repositórios personalizados → `https://github.com/PedroGrilo/ha-continente`, categoria **Integração**.
2. Descarrega **Continente** e reinicia o Home Assistant.
3. Definições → Dispositivos e serviços → Adicionar integração → **Continente**, e inicia sessão
   com a tua conta Continente (escolhe *Iniciar sessão*, não *convidado*).
4. Em *Configurar* podes escolher outra lista `todo`.

O login usa OAuth com registo automático do cliente e PKCE; a sessão renova-se sozinha.
Se for revogada, o Home Assistant pede para voltares a iniciar sessão.

## Como é escolhido o produto de cada artigo

Por esta ordem:

1. **Escolhido** — o produto que escolheste antes para esse nome (`continente.choose_product`).
2. **Habitual** — um dos teus *mais comprados* ou favoritos que comece pelo nome do artigo
   ("Arroz" → o arroz que compras sempre).
3. **Pesquisa** — a melhor correspondência do catálogo, com alternativas.

Os nomes da lista podem ter quantidade e categoria no formato do
[Shopping List Plus Card](https://github.com/PedroGrilo/shopping-list-plus-card):
`2x Massa [massaearroz]` é pesquisado como "Massa", quantidade 2.

## Entidades

| Entidade | Estado | Atributos |
|---|---|---|
| `sensor.continente_estimativa_da_lista_de_compras` | total estimado (€) | `items` (produto, preço, alternativas por artigo), `not_found` |
| `sensor.continente_carrinho` | total do carrinho (€) | `total_items`, `min_order`, `items` |

## Ações

| Ação | O que faz |
|---|---|
| `continente.price_list` | recalcula e devolve os preços da lista |
| `continente.choose_product` | memoriza o produto (`sku`) de um artigo; sem `sku`, esquece a escolha |
| `continente.send_to_basket` | põe a lista (ou `items: [{sku, quantity}]`) no carrinho; `dry_run: true` só mostra |
| `continente.search_product` | pesquisa no catálogo |
| `continente.view_basket` | devolve o carrinho |

`send_to_basket` não é idempotente: cada chamada adiciona outra vez.

## Aviso

Projeto pessoal, sem ligação ao Continente. O servidor MCP do Continente pode mudar sem aviso.
