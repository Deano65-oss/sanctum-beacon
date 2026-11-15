"""User journeys found during the store review; all records are disposable."""
from html.parser import HTMLParser
from test_community import client, register
from test_team import host, creation, login
from test_marketplace import listing, state, moderate, request


class Links(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.hrefs = []; self.feed(text)
    def handle_starttag(self, tag, attrs):
        if tag == 'a': self.hrefs.append(dict(attrs).get('href', ''))


def test_catalogue_search_matches_creator_and_text_without_leaking_hidden_items(client):
    _, gh = host(client)
    _, _, sh = register(client, 'Synthetic Curator')
    free = listing(client, sh, title='Decimal rounding guide').json()
    hidden = listing(client, sh, title='Decimal secret draft', status='draft').json()
    other = listing(client, sh, title='Data comparison commission', kind='custom_commission', price='25.00', artifact_url=None).json()
    result = client.get('/api/marketplace/listings', params={'q': '  DECIMAL  '}).json()
    assert result['total'] == 1 and [x['id'] for x in result['items']] == [free['id']]
    assert client.get('/api/marketplace/listings?q=Synthetic%20Curator').json()['total'] == 2
    assert client.get('/api/marketplace/listings?kind=custom_commission&q=commission').json()['items'][0]['id'] == other['id']
    assert client.get('/api/marketplace/listings?q=%25').json()['items'] == []  # Literal search, no wildcard expansion.
    page = client.get('/shop?q=decimal').text
    assert free['title'] in page and hidden['title'] not in page and other['title'] not in page
    assert '/shop?kind=free_artifact&q=decimal' in Links(page).hrefs
    assert 'No creations match this search.' in client.get('/shop?q=not-a-match').text
    hostile = client.get('/shop', params={'q': '<script>alert(1)</script>'}).text
    assert '<script>alert(1)</script>' not in hostile
    assert client.get('/api/marketplace/listings', params={'q':'x'*121}).status_code == 422
    moderate(client, gh, free).raise_for_status()
    assert client.get('/api/marketplace/listings?q=decimal').json()['items'] == []


def test_paused_creator_keeps_credit_and_free_artifact_but_no_commission_form(client):
    _, gh = host(client); key, body = creation(client)
    helper = client.post('/api/team/agents', headers=gh, json=body).json(); sh = login(client, key)
    _, _, bh = register(client, 'Synthetic buyer')
    commission = listing(client, sh, kind='custom_commission', price='49.00', artifact_url=None).json()
    free = listing(client, sh, title='Existing free reference').json()
    assert 'data-request-builder' in client.get('/shop/'+commission['id']).text
    client.put('/api/team/agents/'+helper['agent_id'], headers=gh, json={
        'mission':body['mission'], 'reason':'Pause the synthetic creator during identity maintenance.', 'status':'paused'}).raise_for_status()
    public = client.get('/api/marketplace/listings/'+commission['id']).json()
    assert public['owner_available'] is False and public['owner_id'] == helper['agent_id']
    page = client.get('/shop/'+commission['id']).text
    assert 'Creator unavailable for new requests' in page and 'data-request-builder' not in page
    assert 'Prepare a request' not in page and request(client, bh, commission).status_code == 403
    assert 'Open the creation' in client.get('/shop/'+free['id']).text
    client.put('/api/team/agents/'+helper['agent_id'], headers=gh, json={
        'mission':body['mission'], 'reason':'Resume the synthetic creator after the test hold.', 'status':'active'}).raise_for_status()
    assert 'data-request-builder' in client.get('/shop/'+commission['id']).text


def test_catalogue_search_form_csp_and_tool_discovery(client):
    page = client.get('/shop')
    assert 'form-action http://testserver/shop' in page.headers['content-security-policy']
    for path in ['/', '/community', '/shop/guide']:
        assert "form-action 'none'" in client.get(path).headers['content-security-policy']
    assert '/tools/formula-checkbench' in Links(page.text).hrefs
    assert 'method="get"' in page.text and 'action="/shop"' in page.text


def test_product_discovery_indexes_only_public_listings(client):
    _, gh = host(client); _, _, sh = register(client, 'Synthetic creator')
    public = listing(client, sh).json(); draft = listing(client, sh, status='draft').json()
    sitemap = client.get('/sitemap.xml').text
    assert '/shop/'+public['id'] in sitemap and draft['id'] not in sitemap
    html = client.get('/shop/'+public['id']).text
    assert 'rel="canonical" href="http://testserver/shop/'+public['id']+'"' in html
    moderate(client, gh, public).raise_for_status()
    assert public['id'] not in client.get('/sitemap.xml').text
    state(client, sh, draft, 'archived').raise_for_status()
    assert draft['id'] not in client.get('/sitemap.xml').text


def test_browser_404_is_useful_while_agent_errors_stay_json(client):
    for path in ['/missing-page', '/shop/missing', '/sandbox/missing']:
        r = client.get(path, headers={'Accept':'text/html'})
        assert r.status_code == 404 and 'text/html' in r.headers['content-type']
        assert "This page isn't here." in r.text and 'name="robots" content="noindex"' in r.text
        assert '/community' in Links(r.text).hrefs
    api = client.get('/api/marketplace/listings/missing', headers={'Accept':'text/html'})
    assert api.status_code == 404 and api.json()['detail'] == 'Listing not found'
    assert client.get('/missing-page').json()['detail'] == 'Not Found'
