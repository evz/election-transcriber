import re
from unicodedata import normalize

def slugify(text, delim=u'_'):
    if text:
        text = unicode(text)
        punct_re = re.compile(r'[\t !"#$%&\'()*\-/<=>?@\[\\\]^_`{|},.:;]+')
        result = []
        for word in punct_re.split(text.lower()):
            word = normalize('NFKD', word).encode('ascii', 'ignore')
            if word:
                result.append(word)
        return unicode(delim.join(result))
    else: # pragma: no cover
        return text