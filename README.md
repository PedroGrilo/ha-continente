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
3. Definições → Dispositivos e serviços → Adicionar integração → **Continente**.
4. Abre o link de login, escolhe *Iniciar sessão* (não *convidado*) e entra com a tua conta.
   No fim o browser vai para `http://localhost/continente-ha?code=…` e mostra um erro — é normal:
   copia esse endereço e cola-o no Home Assistant.
5. Em *Configurar* podes escolher outra lista `todo`.

O Continente só aceita endereços de retorno em `localhost`, por isso o login não volta sozinho
ao Home Assistant e o endereço tem de ser colado à mão. Usa OAuth com registo automático do
cliente e PKCE; a sessão renova-se sozinha e, se for revogada, o HA pede novo login.

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
| `sensor.continente_precos_das_ultimas_compras` | nº de produtos a metade do preço | `items`: por artigo comprado, `price` (hoje), `price_7d` / `price_30d` (média dos últimos 7 / 30 dias), `change_7d` / `change_30d` (%), `discount` (%), `bought` |

## Preços das últimas compras

Os artigos marcados como feitos na lista são as "últimas compras" (os 40 mais recentes). Às 9h e
às 18h (e no arranque) a integração lê o preço de hoje de cada um e guarda-o — o Continente só dá
o preço atual, por isso as médias de 7 e 30 dias vão-se enchendo a partir da instalação.

Quando um produto fica a **50 % ou menos** do preço normal (o que o Continente indicar, senão o
mais alto dos últimos 30 dias), dispara o evento `continente_promocao` com `name`, `product`,
`sku`, `price`, `reference_price`, `discount` e `image` — uma vez por promoção. Exemplo:

```yaml
triggers:
  - trigger: event
    event_type: continente_promocao
actions:
  - action: notify.mobile_app_telemovel
    data:
      title: "🏷️ {{ trigger.event.data.name }} a -{{ trigger.event.data.discount }}%"
      message: "{{ trigger.event.data.product }}: {{ trigger.event.data.price }} €"
```

## Ações

| Ação | O que faz |
|---|---|
| `continente.price_list` | recalcula e devolve os preços da lista |
| `continente.choose_product` | memoriza o produto (`sku`) de um artigo; sem `sku`, esquece a escolha |
| `continente.send_to_basket` | põe a lista (ou `items: [{sku, quantity}]`) no carrinho; `dry_run: true` só mostra |
| `continente.search_product` | pesquisa no catálogo |
| `continente.view_basket` | devolve o carrinho |
| `continente.update_prices` | lê já os preços das últimas compras |
| `continente.get_product` | devolve tudo o que o Continente diz de um produto (`sku`) |

`send_to_basket` não é idempotente: cada chamada adiciona outra vez.

## Aviso

Projeto pessoal, sem ligação ao Continente. O servidor MCP do Continente pode mudar sem aviso.
