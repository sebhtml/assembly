"""
Job router.  Recieves job requests.  Manages data transfer, job queuing.
"""
# Import python libs
import cherrypy
import datetime
import errno
import json
import logging
import pika
import pprint
import os
import re
import sys
from bson import json_util
from ConfigParser import SafeConfigParser
from distutils.version import StrictVersion
from prettytable import PrettyTable
from traceback import format_exc

# Import A-RAST libs
import metadata as meta
import shock
from nexus import client as nexusclient
import ar_client.client as ar_client 

def send_message(body, routingKey):
    """ Place the job request on the correct job queue """

    connection = pika.BlockingConnection(pika.ConnectionParameters(
            host='localhost'))
    channel = connection.channel()
    channel.queue_declare(queue=routingKey, durable=True)
    #channel.basic_qos(prefetch_count=1)
    channel.basic_publish(exchange = '',
                          routing_key=routingKey,
                          body=body,
                          properties=pika.BasicProperties(
                          delivery_mode=2)) #persistant message
    logging.debug(" [x] Sent to queue: %r: %r" % (routingKey, body))
    connection.close()

def send_kill_message(user, job_id):
    """ Place the kill request on the correct job queue """
    ## Set status to killed if not running yet. Otherwise, send.
    job_doc = metadata.get_job(user, job_id)
    uid = job_doc['_id']
    if job_doc['status'] == 'queued':
        metadata.update_job(uid, 'status', 'Terminated')
    elif re.search("Running", job_doc['status']):
        msg = json.dumps({'user':user, 'job_id':job_id})
        connection = pika.BlockingConnection(pika.ConnectionParameters(
                host='localhost'))
        channel = connection.channel()
        channel.exchange_declare(exchange='kill',
                                 type='fanout')
        channel.basic_publish(exchange = 'kill',
                              routing_key='',
                              body=msg,)
        logging.debug(" [x] Sent to kill exchange: %r" % (job_id))
        connection.close()
    else:
        return "Invalid Job ID"

def determine_routing_key(size, params):
    """Depending on job submission, decide which queue to route to."""
    #if params['version'].find('beta'):
     #   print 'Sent to testing queue'
      #  return 'jobs.test'
    try:
        routing_key = params['queue']
    except:
        pass
    if routing_key:
        return routing_key
    return parser.get('rabbitmq','default_routing_key')


def get_upload_url():
    global parser
    return parser.get('shock', 'host')


def route_job(body):
    client_params = json.loads(body) #dict of params
    routing_key = determine_routing_key (1, client_params)
    job_id = metadata.get_next_job_id(client_params['ARASTUSER'])

    if not client_params['data_id']:
        data_id = metadata.get_next_data_id(client_params['ARASTUSER'])
        client_params['data_id'] = data_id
        
    client_params['job_id'] = job_id

    ## Check that user queue limit is not reached

    uid = metadata.insert_job(client_params)
    logging.info("Inserting job record: %s" % client_params)
    metadata.update_job(uid, 'status', 'queued')
    p = dict(client_params)
    metadata.update_job(uid, 'message', p['message'])
    msg = json.dumps(p)

    send_message(msg, routing_key)
    response = str(job_id)
    return response

def register_data(body):
    """ User is only submitting libraries, return data ID """
    client_params = json.loads(body) #dict of params
    data_id = metadata.get_next_data_id(client_params['ARASTUSER'])
    client_params['data_id'] = data_id

    # Inserting a blank job
    uid = metadata.insert_job(client_params)
    logging.info("Inserting data record: %s" % client_params)
    p = dict(client_params)
    metadata.update_job(uid, 'message', p['message'])

    response = json.dumps({"data_id": data_id})
    return response

def authenticate_request():
    if cherrypy.request.method == 'OPTIONS':
        return 'OPTIONS'
    try:
        token = cherrypy.request.headers['Authorization']
    except:
        print "Auth error"
        raise cherrypy.HTTPError(403)
    
    #parse out username
    r = re.compile('un=(.*?)\|')
    m = r.search(token)
    if m:
        user = m.group(1)
    else:
        print "Auth error"
        raise cherrypyHTTPError(403, 'Bad Token')
    auth_info = metadata.get_auth_info(user)
    if auth_info:
        # Check exp date
        auth_time_str = auth_info['token_time']
        atime = datetime.datetime.strptime(auth_time_str, '%Y-%m-%d %H:%M:%S.%f')
        ctime = datetime.datetime.today()
        globus_user = user
        print auth_info
        if (ctime - atime).seconds > 15*60: # 15 min auth token
            print 'expired, reauth'
            nexus = nexusclient.NexusClient(config_file = 'nexus/nexus.yml')
            globus_user = nexus.authenticate_user(token)
            metadata.update_auth_info(globus_user, token, str(ctime))
            
    else:
        nexus = nexusclient.NexusClient(config_file = 'nexus/nexus.yml')
        globus_user = nexus.authenticate_user(token)
        if globus_user:
            metadata.insert_auth_info(globus_user, token,
                                      str(datetime.datetime.today()))
        else:
            raise Exception ('problem authorizing with nexus')
    try:
        if globus_user is None:
            return user
        return globus_user
    except:
        raise cherrypy.HTTPError(403, 'Failed Authorization')
    

def CORS():
    cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
    cherrypy.response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
#    cherrypy.response.headers["Access-Control-Allow-Headers"] = "X-Requested-With"
    cherrypy.response.headers["Access-Control-Allow-Headers"] = "Authorization, origin, content-type, accept"
#    cherrypy.response.headers["Content-Type"] = "application/json"



def start(config_file, mongo_host=None, mongo_port=None,
          rabbit_host=None, rabbit_port=None):
    global parser, metadata
    logging.basicConfig(level=logging.DEBUG)

    parser = SafeConfigParser()
    parser.read(config_file)
    metadata = meta.MetadataConnection(config_file, mongo_host)

    ##### CherryPy ######
    
    #cherrypy.tools.CORS = cherrypy.Tool('before_finalize', CORS)

    conf = {
        'global': {
            'server.socket_host': '0.0.0.0',
            'server.socket_port': 8000,
            'log.screen': True,
            'ar_shock_url': parser.get('shock', 'host'),
            },
    }

    static_root = parser.get('web_serve', 'root')

    root = Root()
    root.user = UserResource()
    root.module = ModuleResource()
    root.shock = ShockResource({"shockurl": get_upload_url()})
    root.static = StaticResource(static_root)


    #cherrypy.request.hooks.attach('before_request_body', authenticate_request)
    cherrypy.request.hooks.attach('before_finalize', CORS)
    cherrypy.quickstart(root, '/', conf)
    ###### DOES IT AUTH EVERY REQUEST??? ########
    

class JobResource:

    @cherrypy.expose
    def new(self, userid=None):
        userid = authenticate_request()
        if userid == 'OPTIONS':
            return ('New Job Request') # To handle initial html OPTIONS requess
        params = json.loads(cherrypy.request.body.read())
        params['ARASTUSER'] = userid
        params['oauth_token'] = cherrypy.request.headers['Authorization']
        return route_job(json.dumps(params))

    @cherrypy.expose
    def kill(self, userid=None, job_id=None):
        if userid == 'OPTIONS':
            return ('New kill Request') # To handle initial html OPTIONS requess
        k = send_kill_message(userid, job_id)
        if k:
            return k
        return 'Kill request sent for job {}'.format(job_id)

    @cherrypy.expose
    def default(self, job_id, *args, **kwargs):
        if len(args) == 0: # /user/USER/job/JOBID/
            pass
        else:
            resource = args[0]
        try:
            userid = kwargs['userid']
        except:
            raise cherrypyHTTPError(403)

        if resource == 'shock_node':
            return self.get_shock_node(userid, job_id)
        elif resource == 'assembly':
            return self.get_assembly_nodes(userid, job_id)
        elif resource == 'report':
            return 'Report placeholder'
        elif resource == 'status':
            return self.status(job_id=job_id, **kwargs)
        elif resource == 'kill':
            user = authenticate_request()
            return self.kill(job_id=job_id, userid=user)
        else:
            return resource
    @cherrypy.expose
    def status(self, **kwargs):
        try:
            job_id = kwargs['job_id']
        except:
            job_id = None
        if job_id: # Single job record
            doc = metadata.get_job(kwargs['userid'], job_id)
            if doc:
                try:
                    if kwargs['format'] == 'json':
                        return json.dumps(doc)
                except:
                    print '[.] CLI request status'

                return doc['status']
            else:
                return "Could not get job status"
            
        else:
            try: 
                records = int(kwargs['records'])
            except:
                records = 100

            docs = metadata.list_jobs(kwargs['userid'])
            pt = PrettyTable(["Job ID", "Data ID", "Status", "Run time", "Description"])
            if docs:
                try:
                    if kwargs['format'] == 'json':
                        return json.dumps(list(reversed(docs[-records:]))); 
                except:
                    print '[.] CLI request status'

                for doc in docs[-records:]:
                    row = [doc['job_id'], str(doc['data_id']), doc['status'][:40],]
                    try:
                        row.append(str(doc['computation_time']))
                    except:
                        row += ['']
                    try:
                        row.append(str(doc['message']))
                    except:
                        row += ['']
                    pt.add_row(row)
                return pt.get_string() + "\n"


    def get_shock_node(self, userid=None, job_id=None):
        """ GET /user/USER/job/JOB/node """
        if not job_id:
            raise cherrypy.HTTPError(403)
        doc = metadata.get_job(userid, job_id)
        try:
            result_data = doc['result_data']
        except:
            raise cherrypy.HTTPError(500)
        return json.dumps(result_data)

    def get_assembly_nodes(self, userid=None, job_id=None):
        if not job_id:
            raise cherrypy.HTTPError(403)
        doc = metadata.get_job(userid, job_id)
        try:
            result_data = doc['contig_ids']
        except:
            raise cherrypy.HTTPError(500)
        return json.dumps(result_data)

class StaticResource:

    def __init__(self, static_root):
        self.static_root = static_root
        self._cp_config = {'tools.staticdir.on' : True,
                           'tools.staticdir.dir': self.static_root}

    @cherrypy.expose
    def serve(self, userid=None, job_id=None, **kwargs):
        # if userid == 'OPTIONS':
        #     return ('New Job Request') # To handle initial html OPTIONS requess
        #Return data id
        try:
            token = cherrypy.request.headers['Authorization']
        except:
            token = None
        aclient = ar_client.Client('localhost', userid, token)
        print cherrypy.config
        outdir = os.path.join(self.static_root, userid, job_id)

        try:
            os.makedirs(outdir)
        except OSError, e:
            # be happy if someone already created the path
            if e.errno != errno.EEXIST:
                raise
        ## Get all data
        
        aclient.get_job_data(job_id=job_id, outdir=outdir)
        return 'done'
#    serve._cp_config = {'tools.staticdir.on' : False}

class FilesResource:
    @cherrypy.expose
    def default(self, userid=None):
        testResponse = {}
        return '{}s files!'.format(userid)

class DataResource:
    @cherrypy.expose
    def new(self, userid=None):
        userid = authenticate_request()
        if userid == 'OPTIONS':
            return ('New Job Request') # To handle initial html OPTIONS requess
        params = json.loads(cherrypy.request.body.read())
        params['ARASTUSER'] = userid
        params['oauth_token'] = cherrypy.request.headers['Authorization']
        #Return data id
        return register_data(json.dumps(params))

    @cherrypy.expose
    def default(self, data_id=None, userid=None):
        ## /user/USERID/data/
        if not data_id:
            docs = metadata.get_docs_distinct_data_id(userid)
            summarized_docs = []
            for d in docs: ## return summarized docs
                summarized_docs.append({k: d[k] for k in ('data_id', 'filename')})
            return json.dumps(summarized_docs)
        ## /user/USERID/data/            
        doc = metadata.get_doc_by_data_id(data_id, userid)
        status = {k: doc[k] for k in ('ids', 'data_id', 'filename', 'file_sizes', 
                                      'single', 'pair', 'version')}
        return json.dumps(status)


        
class UserResource(object):
    @cherrypy.expose
    def new():
        pass

    @cherrypy.expose
    def default(self):
        return 'user default ok'

    default.job = JobResource()
    default.files = FilesResource()
    default.data = DataResource()

    # Pull user id from URL
    def __getattr__(self, name):
        if name is not ('_cp_config'): #assume username
            cherrypy.request.params['userid'] = name
            return self.default
        raise AttributeError("%r object has no attribute %r" % (self.__class__.__name__, name))


class StatusResource:
    def current(self):
        json_request = cherrypy.request.body.read()
        return route_job(json_request)

class ModuleResource:
    @cherrypy.expose
    def default(self, module_name="avail", *args, **kwargs):
        print module_name
        if module_name == 'avail' or module_name == 'all':
            with open(parser.get('web', 'ar_modules')) as outfile:
                return outfile.read()
        return module_name
        
class ShockResource(object):

    def __init__(self, content):
        self.content = content

    @cherrypy.expose
    def default(self):
        return json.dumps(self.content)


class Root(object):
    @cherrypy.expose
    def default(self):
        print 'root'

