from django.template import Context, Template
from django.utils.encoding import smart_str

from pages.models import Page, PageFile


def run(*args, **kwargs):
    print "Populating page card thumbnails & memcached..."

    counted = set()
    for p in PageFile.objects.all().select_related('region'):
        if (p.slug, p.region) in counted:
            continue
        counted.add((p.slug, p.region))

    for (slug, region) in counted:
        p = Page.objects.filter(slug=slug, region=region)
        if not p:
            continue
        p = p[0]

        print "Generating for %s" % smart_str(p)
        try:
            t = Template("""{% load thumbnail %}
{% load cards_tags %}

{% page_card page as card %}
  
{% if card.file %}
  {% thumbnail card.file.file "200x200" crop="center" as im %}
    <img src="{{ im.url }}"/>
  {% endthumbnail %}
{% endif %}
""")
            t.render(Context({'page': p}))
        except:
            print "ERROR on", smart_str(p)
