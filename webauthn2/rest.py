
# 
# Copyright 2012 University of Southern California
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
Webauthn2 REST library supports dispatch from web.py applications.

A factory model encapsulates an entire webauthn2 security context
Manager instance that can be reused by the per-request message handler
classes expected by web.py:

  webauthn2factory = RestHandlerFactory()

  urls = (
     ...
     '/myapp/session(/[^/]+)',  webauthn2factory.UserSession,
     '/myapp/password(/[^/]+)', webauthn2factory.UserPassword,
     '/myapp/user(/[^/]+)', webauthn2factory.UserManage,
     '/myapp/attribute(/[^/]+)', webauthn2factory.AttrManage,
     '/myapp/user/([^/]+)/attribute(/[^/]+), webauthn2factory.AttrAssign,
     '/myapp/attribute/([^/]+)/implies(/[^/]+), webauthn2factory.AttrNest
  )

These REST handlers use basic form/URI inputs and return only basic
URI or JSON results to support AJAX clients.  An application MAY
expose these REST APIs for where appropriate, including its own
AJAX UI front-end to these features.

The handler base class webauthn2factory.RestHandler can be extended by
an application in order to get an integrated security manager with an
optimized database connection pooling feature:

  class AppHandler (webauthn2factory.RestHandler):

     def __init__(self):
         webauthn2factory.RestHandler.__init__(self)

     def GET(self):
         def db_body(db):
            self.context = self.manager.get_request_context(db=db)
            ... # other application use of db
         return self._db_wrapper(db_body)

But this support class is entirely optional.  An application can also
just use the Manager instance directly in its own message handlers:

  manager = webauthn2factory.manager

  class AppHandler (object):

     def GET(self):
        self.context = manager.get_request_context()

"""


from util import *
from manager import Manager, Context
from providers import Session
import re

import web

class RestHandlerFactory (object):
    """
    RestHandlerFactory encapsulates one-time application startup.

    """

    def __init__(self, manager=None, overrides=None, defaults=None):
        """
        Initialize request handler factory, with optional pre-initialized service configuration.

        If manager is not None, it is a pre-constructed instance of
        the webauthn2.Manager class.

        If manager is None, the RestHandler instance constructs its
        own Manager instance, passing overrides and defaults verbatim.

        The REST service layer finds its own configuration within
        manager.config

        """
        if not manager:
            manager = Manager(overrides, defaults)

        session_uri = manager.config.get('handler_uri_usersession', None)
        session_duration = datetime.timedelta(minutes=int(manager.config.get('session_expiration_minutes', 30)))

        class RestHandler (DatabaseConnection):
            """
            RestHandler is a base class suitable for use as a web.py request handler.

            It initializes its self.manager and its parent class
            DatabaseConnection.  Derived application classes must
            implement their own web methods PUT, GET, etc. and
            initialize their own self.context such as:

               self.context = self.manager.get_request_context(db=db)

            if they already have opened a pooled connection, or:

               self.context = self.manager.get_request_context()

            if the handler is not already connecte to the database.

            """
            def __init__(self):
                DatabaseConnection.__init__(self, manager.config)
                self.manager = manager

        class UserSession (RestHandler):
            """
            UserSession is a RESTful login/logout handler.

            Register it at a web.py URI pattern like:

               "your_session_prefix(/?)"
               "your_session_prefix(/[^/]+)"

            so its methods recieve one positional argument with a URI
            fragment containing an explicit session ID prefixed with
            the '/' character.
            
            """
            def __init__(self):
                RestHandler.__init__(self)
                self.session_uri = session_uri
                self.session_duration = session_duration

            def POST(self, sessionids, storage=None):
                """
                Session start (login) uses POST with form parameters.

                It does not make sense to POST to an existing
                sessionid, only to the session container as a
                whole. We treat implied session IDs, e.g. from
                cookies, the same way.

                We also refuse to support login if the client is
                already authenticated in some manner.

                """
                if sessionids:
                    # trim leading '/'
                    sessionids = sessionids[1:]

                if sessionids:
                    # no POST support for session ID URLs
                    raise NoMethod()

                if not self.manager.clients.login \
                        or not self.manager.sessionids \
                        or not self.manager.sessions:
                    # the provider config doesn't support login sessions
                    raise NoMethod()

                if not storage:
                    storage = web.input()

                return self._login_get_or_post(storage)

            def _session_authz(self, sessionids, get_html=False):
                if not self.manager.sessionids \
                        or not self.manager.sessions:
                    # the provider config doesn't support sessions
                    raise NoMethod()
            
                if not self.context.session:
                    if get_html:
                        # return as no-op and let caller deal with it
                        return
                    else:
                        raise NotFound('existing session')

                if sessionids:
                    # format is /key,... so unpack
                    sessionids = [ urlunquote(i) for i in sessionids[1:].split(',') ]

                    for uri_key in sessionids:
                        if uri_key not in self.context.session.keys:
                            raise Forbidden('third-party session access for key "%s" forbidden' % uri_key)
                        
            def GET(self, sessionids, db=None):
                """
                Session status uses GET.

                We require sessionids from message context, and allow
                the same sessionids in the URI for RESTful
                interactions but only on the client's own current session.

                Future versions may allow third-party session
                inspection with authz.

                Optional db parameter allows delegation to this method
                from within another message handler already managing
                database transactions.

                """

                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    self._session_authz(sessionids, get_html=True)

                if db:
                    db_body(db)
                else:
                    self._db_wrapper(db_body)

                # just report on current session status
                content_type = negotiated_content_type(
                    ['application/json', 'text/html'],
                    'application/json'
                    )

                def has_login_params():
                    for p in web.input():
                        if p != 'referrer':
                            return True
                    return False

                if self.context.session is None:
                    if self.manager.clients.login is not None:
                            if self.manager.clients.login.accepts_login_get() and has_login_params():
                                return self._login_get_or_post(web.input())
                            elif self.manager.preauth is not None:
                                preauth_info = self.manager.preauth.preauth_info(self.manager, self.context, db)
                                if preauth_info != None:
                                    return preauth_info
                    if content_type == 'text/html' \
                            and self.manager.clients.login is not None \
                            and self.session_uri is not None:
                        # return a basic HTML form for bootstrapping API servers
                        params = web.input()
                        body = ("""<!DOCTYPE html>
<html>
<body>
<h1>Log in</h1>
%(error)s<form action="%(uri)s" method="post">
%(inputs)s
</form>
</body>
</html>
""" % dict(uri=self.session_uri,
           error=params.get('error') and '<p>%s</p>' % params.get('error') or '',
           inputs="\n".join(['<p>%(name)s: <input type="%(type)s" name="%(name)s" /></p>' % dict(
                                            name=name,
                                            type=name.lower().find('password') > -1 and 'password' or 'text'
                                            )
                             for name in self.manager.clients.login.login_keywords()
                             ] + 
                            ['<input type="submit" value="Login" />',
                             '<input type="hidden" name="referrer" value="%(refer)s" />' % dict(
                                            refer=params.get('referrer', '')
                                            )
                             ])
           )
                                )
                        web.ctx.status = '401 Unauthorized'
                        web.header('Content-Type', 'text/html')
                        web.header('Content-Length', '%d' % len(body))
                        return body
                    else:
                        raise NotFound('No existing login session found.')

                # do not include sessionids since we don't want to enable
                # any XSS attack where a hidden cookie can be turned into an 
                # explicit session token by an untrusted AJAX client lib...?
                now = datetime.datetime.now(pytz.timezone('UTC'))
                response = dict(
                    client=self.context.client,
                    attributes=list(self.context.attributes),
                    since=self.context.session.since,
                    expires=self.context.session.expires,
                    seconds_remaining=self.context.session.expires and (self.context.session.expires - now).seconds
                    )
                response = jsonWriter(response) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def PUT(self, sessionids):
                """
                Session extension uses PUT.

                We require sessionids from message context, and allow
                the same sessionids in the URI for RESTful
                interactions but only on the client's own current session.

                Future versions may allow third-party session
                extension with authz.

                """
                # just extend session and then act like GET
                now = datetime.datetime.now(pytz.timezone('UTC'))

                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    self._session_authz(sessionids)
                    self.context.session.expires = now + self.session_duration
                    self.manager.sessions.extend(self.manager, self.context, db)
                    return self.GET(sessionids, db)

                return self._db_wrapper(db_body)

            def DELETE(self, sessionids):
                """
                Session termination uses DELETE.

                We require sessionids from message context, and allow
                the same sessionids in the URI for RESTful
                interactions but only on the client's own current session.

                Future versions may allow third-party session
                extension with authz.

                """
                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    self._session_authz(sessionids)
                    self.manager.sessions.terminate(self.manager, self.context, db)

                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

            def _login_get_or_post(self, storage):
                for key in self.manager.clients.login.login_keywords():
                    if key not in storage:
                        raise BadRequest('missing required parameter "%s"' % key)

                referrer = storage.get('referrer')

                def db_body(db):
                    self.context = Context(self.manager, False, db)

                    if self.context.session or self.context.client:
                        raise Conflict('Login request conflicts with current client authentication state.')

                    self.context.session = Session()

                    # allocate new session ID first
                    self.manager.sessionids.create_unique_sessionids(self.manager, self.context)

                    try:
                        # perform authentication
                        self.context.client = self.manager.clients.login.login(self.manager, self.context, db, **storage)
                    except (KeyError, ValueError), ev:
                        # we don't reveal detailed reason for failed login 
                        msg = 'session establishment with (%s) failed' \
                            % ', '.join(self.manager.clients.login.login_keywords(True))
                        if referrer and self.session_uri:
                            web.ctx.status = '303 See Other'
                            web.header('Location', '%s?referrer=%s&error=%s' % (
                                    self.session_uri, 
                                    urlquote(referrer),
                                    urlquote(msg))
                                       )
                            return None
                        else:
                            raise Unauthorized(msg)

                    if self.manager.attributes.client:
                        # dig up attributes for client
                        self.manager.attributes.client.set_msg_context(self.manager, self.context, db)

                    # try to register new session
                    self.manager.sessions.new(self.manager, self.context, db)
                    return True

                # run entire sequence in a restartable db transaction
                result = self._db_wrapper(db_body)
                if result is None:
                    return
                
                # build response
                self.manager.sessionids.set_request_sessionids(self.manager, self.context)
                uri = self.session_uri
                keys = ','.join([ urlquote(i) for i in self.context.session.keys ])
                if uri:
                    uri += '/' + keys
                else:
                    uri = keys

                if self.manager.preauth != None:
                    preauth_referrer = self.manager.preauth.preauth_referrer()
                    if preauth_referrer != None:
                        web.ctx.status = '303 See Other'
                        web.header('Location', preauth_referrer)
                        return ''

                if 'env' in web.ctx:
                    if referrer:
                        web.ctx.status = '303 See Other'
                        web.header('Location', referrer)
                        return ''
                    else:
                        web.ctx.status = '201 Created'
                        web.header('Content-Type', 'text/uri-list')
                        web.header('Content-Length', len(keys) + 1)
                        web.header('Location', uri)
                        return keys + '\n'

               
        class UserPassword (RestHandler):
            """
            UserPassword is a RESTful password management handler.

            Register it at a web.py URI pattern like:

               "your_passwd_prefix(/?)"
               "your_passwd_prefix(/[^/]+)"

            so its methods recieve one positional argument with a URI
            fragment containing an explicit user ID.
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def _password_prep(self, userids):
                if not self.manager.clients.passwd:
                    # the provider config doesn't support passwords
                    raise NoMethod()

                if userids:
                    # format is /user,...
                    userids = set([ urlunquote(i) for i in userids[1:].split(',') ])
                elif self.context.client:
                    userids = [ self.context.client ]
                else:
                    raise BadRequest('password management requires target userid')

                return userids

            def PUT(self, userids, storage=None):
                """
                Password update uses PUT.

                Input form/query parameters:
                  password: new password to set, or missing to generate new random one
                  old_password: existing password for verification

                We require client ID from message context, and allow
                userid(s) from REST URI.  Authorized admins can manage
                other user passwords, and regular users can manage
                their own only with old_password.

                Successful response is a JSON object summarizing
                results per input userid: True or a randomly generated
                password for that user.

                On errors with multiple userids, some passwords may
                already be updated.

                """
                if not storage:
                    storage = web.input()
                password = storage.get('password', None)
                old_password = storage.get('old_password', None)

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()
                        
                    new_passwords = dict()
                    for userid in self._password_prep(userids):
                        try:
                            new_passwords[userid] = self.manager.clients.passwd.create(self.manager,
                                                                                       self.context,
                                                                                       userid,
                                                                                       password,
                                                                                       old_password,
                                                                                       db)
                        except KeyError, ev:
                            # this is only raised by password provider if authorized
                            raise NotFound('user "%s"' % userid)
                        except ValueError, ev:
                            raise Forbidden('update of password for user "%s" forbidden' % userid)
                    return new_passwords
        
                response = jsonWriter(self._db_wrapper(db_body)) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def DELETE(self, userids, storage=None):
                """
                Password disable uses DELETE.

                Input form/query parameter:
                  old_password: existing password for verification

                We require client ID from message context, and allow
                userid(s) from REST URI.  Authorized admins can manage
                other user passwords, and regular users can manage
                their own only with old_password.

                Successful response is empty.

                On errors with multiple userids, some passwords may
                already be disabled.

                """
                if not storage:
                    storage = web.input()
                old_password = storage.get('old_password', None)

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for userid in self._password_prep(userids):
                        try:
                            self.manager.clients.passwd.delete(self.manager,
                                                                 self.context,
                                                                 userid,
                                                                 old_password,
                                                                 db)
                        except KeyError, ev:
                            # this is only raised by password provider if authorized
                            raise NotFound('user "%s"' % userid)
                        except ValueError, ev:
                            raise Forbidden('delete of password for user "%s" forbidden' % userid)
    
                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

        class UserManage (RestHandler):
            """
            UserManage is a RESTful user identity management handler.

            Register it at a web.py URI pattern like:

               "your_user_prefix(/?)"
               "your_user_prefix(/[^/]+)"

            so its methods recieve one positional argument with a URI
            fragment containing an explicit user ID.
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def GET(self, userids, storage=None):
                """
                User identity listing uses GET.

                We require client ID from message context, and allow
                userid(s) from REST URI.  Authorized clients can list
                other user identities, and regular users can always
                view their own.

                Successful response is a JSON object summarizing
                user(s).

                """
                if userids:
                    # format is /user,...
                    userids = set([ urlunquote(i) for i in userids[1:].split(',') ])
                else:
                    userids = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    if not self.manager.clients.search:
                        if not userids \
                                or userids.difference( set([ c for c in [self.context.client] if c ]) ):
                            raise Conflict('Server does not support listing of other client identities.')

                    if not userids:
                        # request without userids means list all users
                        clients = self.manager.clients.search.get_all_clients(self.manager, self.context)
                        response = clients and list(clients)
                    elif userids.difference( set([ c for c in [self.context.client] if c ]) ):
                        # request with userids means list only specific users other than self
                        clients = self.manager.clients.search.get_all_clients(self.manager, self.context)
                        if clients and userids.difference( clients ):
                            raise NotFound('Some client identities not found: %s.' % ', '.join(userids.difference( clients )))
                        response = clients and list(clients)
                    else:
                        # request with userid equal to self.context.client can be answered without search API
                        assert len(userids) == 1
                        assert userids[0] == self.context.client
                        response = [ self.context.client ]

                    if response == None:
                        raise ValueError()

                    return response

                try:
                    response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                except ValueError:
                    raise Forbidden('listing of other client identities forbidden')

                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def PUT(self, userids, storage=None):
                """
                User identity creation uses PUT.

                We require client ID from message context, and require
                userid(s) from REST URI.  Authorized admins can create
                user identity.
                
                Successful response is a JSON list of all
                requested clients (as relative URIs, e.g. bare client
                names).

                """
                if userids:
                    # format is /user,...
                    userids = set([ urlunquote(i) for i in userids[1:].split(',') ])
                else:
                    userids = set()

                if not self.manager.clients.manage:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for userid in userids:
                        try:
                            self.manager.clients.manage.create(self.manager,
                                                               self.context,
                                                               userid,
                                                               db)
                        except ValueError, ev:
                            raise Forbidden('creation of client identity forbidden')

                    return list(userids)
        
                response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def DELETE(self, userids, storage=None):
                """
                User identity removal uses DELETE.

                We require client ID from message context, and require
                userid(s) from REST URI.  Authorized admins can delete
                user identity.
                
                """
                if userids:
                    # format is /user,...
                    userids = set([ urlunquote(i) for i in userids[1:].split(',') ])
                else:
                    userids = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for userid in userids:
                        try:
                            self.manager.clients.manage.delete(self.manager,
                                                               self.context,
                                                               userid,
                                                               db)
                        except KeyError, ev:
                            # this is only raised by password provider if authorized
                            raise NotFound('user "%s"' % userid)
                        except ValueError, ev:
                            raise Forbidden('delete of client identity forbidden')
    
                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

        class AttrManage (RestHandler):
            """
            AttrManage is a RESTful attribute management handler.

            Register it at a web.py URI pattern like:

               "your_attr_prefix(/?)"
               "your_attr_prefix(/[^/]+)"

            so its methods recieve one positional argument with a URI
            fragment containing an explicit attr ID.
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def GET(self, attrs, storage=None):
                """
                Attribute listing uses GET.

                We require client ID from message context, and allow
                attrid(s) from REST URI.  Authorized clients can list
                other attributes, and regular users can always
                view their own.

                Successful response is a JSON object summarizing
                user(s).

                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    if not self.manager.attributes.search:
                        if not attrs \
                                or attrs.difference( self.context.attributes and self.context.attributes or set() ):
                            raise Conflict('Server does not support listing of other attributes.')

                    if not attrs:
                        # request without attrs means list all attrs
                        response = list(self.manager.attributes.search.get_all_attributes(self.manager, self.context, db, False))
                    elif self.manager.attributes.search:
                        # request with attrs means list only specific attrs
                        allattrs = set(self.manager.attributes.search.get_all_attributes(self.manager, self.context, db, False))
                        if attrs.difference( allattrs ):
                            raise NotFound('Some attributes not found: %s.' % ', '.join(attrs.difference( allattrs )))
                        response = list(attrs)
                    else:
                        # request with attrs subsetting self.context.attributes can be answered without search API
                        # we would have already raised Conflict above if it wasn't a proper subset
                        response = list(attrs)

                    return response

                try:
                    response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                except ValueError:
                    raise Forbidden('listing of other attributes forbidden')

                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def PUT(self, attrs, storage=None):
                """
                Attribute creation uses PUT.

                We require client ID from message context, and require
                attr(s) from REST URI.  Authorized admins can create
                attributes.
                
                Successful response is a JSON list of all
                requested attributes (as relative URIs, e.g. bare attribute
                names).

                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                if not self.manager.attributes.manage:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for attr in attrs:
                        try:
                            self.manager.attributes.manage.create(self.manager,
                                                                  self.context,
                                                                  attr,
                                                                  db)
                        except ValueError, ev:
                            raise Forbidden('creation of attribute forbidden')

                    return list(attrs)
        
                response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def DELETE(self, attrs, storage=None):
                """
                Attribute removal uses DELETE.

                We require client ID from message context, and require
                attr(s) from REST URI.  Authorized admins can delete
                attributes.
                
                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for attr in attrs:
                        try:
                            self.manager.attributes.manage.delete(self.manager,
                                                                  self.context,
                                                                  attr,
                                                                  db)
                        except KeyError, ev:
                            # this is only raised by password provider if authorized
                            raise NotFound('attribute "%s"' % attr)
                        except ValueError, ev:
                            raise Forbidden('delete of attribute forbidden')
    
                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

        class AttrAssign (RestHandler):
            """
            AttrAssign is a RESTful attribute assignment management handler.

            Register it at a web.py URI pattern like:

               "your_user_prefix/([^/]+)/attribute(/[^/]+)"

            so its methods recieve one positional argument with a URI
            fragment containing an explicit user ID and one positional
            argument with a URI fragment containing an attribute list.
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def GET(self, userid, attrs, storage=None):
                """
                Attribute assignment listing uses GET.

                We require client ID from message context and REST URI
                and allow attrid(s) from REST URI.  Authorized clients
                can list other users' attributes, and regular users
                can always view their own.

                Successful response is a JSON object summarizing
                user(s).

                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    if not self.manager.attributes.assign:
                        if userid != self.context.client:
                            raise Conflict('Server does not support listing of other user attributes.')
                        # fall back behavior only if provider API isn't available
                        allattrs = self.context.attributes
                    else:
                        allattrs = self.manager.attributes.assign.list(self.manager, self.context, userid, db)
    
                    if not attrs:
                        # request without attrs means list all of user's attrs
                        response = list(allattrs)
                    else:
                        # request with attrs means list only specific attrs
                        if attrs.difference( allattrs ):
                            raise NotFound('Some attributes not assigned: %s.' % ', '.join(attrs.difference( allattrs )))
                        response = list(attrs)

                    return response

                try:
                    response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                except ValueError:
                    raise Forbidden('listing of user attributes forbidden')

                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def PUT(self, userid, attrs, storage=None):
                """
                Attribute assignment creation uses PUT.

                We require client ID from message context, and require
                attr(s) from REST URI.  Authorized admins can create
                attributes.
                
                Successful response is a JSON list of all
                requested attributes (as relative URIs, e.g. bare attribute
                names).

                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                if not self.manager.attributes.assign:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for attr in attrs:
                        try:
                            self.manager.attributes.assign.create(self.manager,
                                                                  self.context,
                                                                  attr,
                                                                  userid,
                                                                  db)
                        except ValueError, ev:
                            raise Forbidden('creation of attribute assignment forbidden')

                    return list(attrs)
        
                response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def DELETE(self, userid, attrs, storage=None):
                """
                Attribute removal uses DELETE.

                We require client ID from message context, and require
                attr(s) from REST URI.  Authorized admins can delete
                attributes.
                
                """
                if attrs:
                    # format is /attr,...
                    attrs = set([ urlunquote(i) for i in attrs[1:].split(',') ])
                else:
                    attrs = set()

                if not self.manager.attributes.assign:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for attr in attrs:
                        try:
                            self.manager.attributes.assign.delete(self.manager,
                                                                  self.context,
                                                                  attr,
                                                                  userid,
                                                                  db)
                        except KeyError, ev:
                            # this is only raised by password provider if authorized
                            raise NotFound(str(ev))
                        except ValueError, ev:
                            raise Forbidden('delete of attribute assignment forbidden')
    
                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

        class AttrNest (RestHandler):
            """
            AttrNest is a RESTful attribute nesting management handler.

            Register it at a web.py URI pattern like:

               "your_attr_prefix/([^/]+)/implies(/[^/]+)"

            so its methods recieve first positional argument with a
            URI fragment containing an explicit attribute ID and one
            positional argument with a URI fragment containing an
            attribute list of extra implied (parent) attributes.
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def GET(self, child, parents, storage=None):
                """
                Attribute nesting listing uses GET.

                We require client ID from message context and child
                attribute from REST URI and allow parent attrid(s)
                from REST URI.  Authorized clients can list other
                attribute nesting.

                Successful response is a JSON object summarizing
                attributes.

                """
                if parents:
                    # format is /attr,...
                    parents = set([ urlunquote(i) for i in parents[1:].split(',') ])
                else:
                    parents = set()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    if not self.manager.attributes.nest:
                        raise Conflict('Server does not support listing of attribute nesting.')

                    allparents = self.manager.attributes.nest.list(self.manager, self.context, child, db)
    
                    if not parents:
                        # request without parents means list all of child's parents
                        response = list(allparents)
                    else:
                        # request with parents means list only specific parents
                        if parents.difference( allparents ):
                            raise NotFound('Some attributes not implied: %s.' % ', '.join(parents.difference( allparents )))
                        response = list(parents)

                    return response

                try:
                    response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                except KeyError:
                    raise NotFound('attribute not found')
                except ValueError:
                    raise Forbidden('listing of nested/implied attributes forbidden')

                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def PUT(self, child, parents, storage=None):
                """
                Attribute nesting creation uses PUT.

                We require client ID  from message context and child
                attribute from REST URI and allow parent attrid(s)
                from REST URI.  Authorized clients can create
                attribute nesting.
                
                Successful response is a JSON list of all
                requested attributes (as relative URIs, e.g. bare attribute
                names).

                """
                if parents:
                    # format is /attr,...
                    parents = set([ urlunquote(i) for i in parents[1:].split(',') ])
                else:
                    parents = set()

                if not self.manager.attributes.nest:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for parent in parents:
                        try:
                            self.manager.attributes.nest.create(self.manager,
                                                                self.context,
                                                                parent,
                                                                child,
                                                                db)
                        except ValueError, ev:
                            raise Forbidden('creation of attribute nesting forbidden')

                    return list(parents)
        
                response = jsonWriter( self._db_wrapper(db_body) ) + '\n'
                if 'env' in web.ctx:
                    web.ctx.status = '200 OK'
                    web.header('Content-Type', 'application/json')
                    web.header('Content-Length', len(response))
                return response

            def DELETE(self, child, parents, storage=None):
                """
                Attribute nest removal uses DELETE.

                We require client ID from message context and child
                attribute from REST URI and allow parent attrid(s)
                from REST URI.  Authorized clients can delete
                attribute nesting.
                
                """
                if parents:
                    # format is /attr,...
                    parents = set([ urlunquote(i) for i in parents[1:].split(',') ])
                else:
                    parents = set()

                if not self.manager.attributes.nest:
                    raise NoMethod()

                def db_body(db):
                    try:
                        self.context = self.manager.get_request_context(db=db)
                    except (ValueError, IndexError):
                        raise Unauthorized()

                    for parent in parents:
                        try:
                            self.manager.attributes.nest.delete(self.manager,
                                                                self.context,
                                                                parent,
                                                                child,
                                                                db)
                        except KeyError, ev:
                            raise NotFound(str(ev))
                        except ValueError, ev:
                            raise Forbidden('delete of attribute nesting forbidden')
    
                self._db_wrapper(db_body)
                if 'env' in web.ctx:
                    web.ctx.status = '204 No Content'
                return ''

        class Preauth (RestHandler):
            """
            Preauth is a RESTful pre-authentication handler.

            Register it at a web.py URI pattern like:

               "your_preauth_prefix(/?)"
               "your_preauth_prefix(/[^/]+)"
            
            """
            def __init__(self):
                RestHandler.__init__(self)

            def GET(self, db=None):
                """
                Return pre-authentication data (e.g., display a web form for users to select among IdPs).
                """
                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    # Should probably fail or something if the user is logged in, but for now we won't bother

                if db:
                    db_body(db)
                else:
                    self._db_wrapper(db_body)
                
                return self.manager.preauth.preauth_info(self.manager, self.context, db)

            def POST(self, db=None):
                """
                Perform pre-authentication tasks (e.g., cache pre-authentication information)
                """
                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    # Should probably fail or something if the user is logged in, but for now we won't bother

                if db:
                    db_body(db)
                else:
                    self._db_wrapper(db_body)

                return self.manager.preauth.preauth_initiate_login(self.manager, self.context, db)

            def DELETE(self, db=None):
                """
                Perform pre-authentication tasks (e.g., cache pre-authentication information)
                """
                def db_body(db):
                    self.context = Context(self.manager, False, db)
                    # Should probably fail or something if the user is logged in, but for now we won't bother

                if db:
                    db_body(db)
                else:
                    self._db_wrapper(db_body)

                self.manager.preauth.preauth_delete(self.manager, self.context, db)

        # make these classes available from factory instance
        self.RestHandler = RestHandler
        self.UserSession = UserSession
        self.UserPassword = UserPassword
        self.UserManage = UserManage
        self.AttrManage = AttrManage
        self.AttrAssign = AttrAssign
        self.AttrNest = AttrNest
        self.Preauth = Preauth



