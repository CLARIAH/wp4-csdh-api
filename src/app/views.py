# -*- coding: utf-8 -*-
from flask import render_template, request, jsonify
from flask.ext.socketio import emit
from flask_swagger import swagger
from werkzeug.exceptions import HTTPException

import traceback
import logging
import requests
import json
import os
import gevent.subprocess as sp

import iribaker
from SPARQLWrapper import SPARQLWrapper, JSON
from rdflib import Graph
from collections import OrderedDict

import config
import util.sparql_client as sc
import util.file_client as fc
import util.git_client as git_client

from app import app, socketio

import loader.reader
import datacube.converter

log = app.logger
log.setLevel(logging.DEBUG)


@app.route('/')
def index():
    return render_template('base.html')

@app.route('/api-docs')
def apidocs():
    return render_template('api-docs.html')

@app.route('/specs')
def specs():
    """
    Provides Swagger specification for the CSDH API
    ---
    tags:
        - Base
    responses:
        '200':
          description: Swagger specification
          type: object
        default:
          description: Unexpected error
          schema:
            $ref: "#/definitions/Message"
    """
    swag = swagger(app)
    swag['info']['version'] = "0.0.1"
    swag['info']['title'] = "CSDH API"
    swag['info']['description'] = "API Specification for the CLARIAH Structured Data Hub"
    # swag['host'] = "api.clariah-sdh.eculture.labs.vu.nl"
    swag['host'] = app.config['SERVER_NAME']
    swag['schemes'] = ['http']
    swag['basePath'] = '/'
    swag['swagger'] = '2.0'

    return jsonify(swag)


@app.route('/trigger',methods=['POST'])
def follow_github():
    """
    Responds to triggers initiated by GitHub webhooks (if activated in the configuration)
    Make sure to set the appropriate parameters in `config.py`
    ---
      tags:
        - Base
      consumes:
        - text/json
      parameters:
        - name: data
          in: body
          description: The GitHub webhook payload
          required: true
          type: object
      responses:
        '200':
          description: Git repository updated
          type: object
          schema:
            $ref: "#/definitions/Message"
        default:
          description: Unexpected error
          schema:
            id: Message
            type: object
            properties:
              code:
                type: integer
                format: int32
              message:
                type: string
    """
    if not config.FOLLOW_GITHUB:
        raise(Exception("This application is not setup to respond to GitHub webhook data"))

    # Retrieve the data from POST
    data = json.loads(request.data)
    log.debug(data)
    # Check whether the data is about the repository & branch we're trying to track
    if (str(data['ref']) != config.FOLLOW_REF or str(data['repository']['url']) != config.FOLLOW_REPO):
        raise(Exception("This application is not setup to respond to pushes to this particular repository or branch"))

    log.info("New commit by: {}".format(data['commits'][0]['author']['name']))
    log.info("Updating code repo")

    # Run the git pull command from the `src` directory (one up)
    message = sp.check_output(['git', 'pull'], cwd='..')

    # Format a response
    response = {'message': message, 'code': 200}
    return jsonify(response)


# Socket IO handlers
@socketio.on('message', namespace='/inspector')
def message(json):
    log.debug('SocketIO message:\n' + str(json))


@app.errorhandler(Exception)
def error_response(ex):
    """
    Handles any errors raised in the execution of the backend
    Builds a JSON representation of the error messages, to be handled by the client
    Complies with the Swagger definnition of an error response
    """
    if 'code' in ex:
        code = ex.code
    elif 'errno' in ex:
        code = ex.errno
    else:
        code = 42
    response = jsonify(message=str(ex), code=code)
    response.status_code = (code
                            if isinstance(ex, HTTPException)
                            else 500)

    log.error(traceback.format_exc())
    return response


@app.route('/dataset/definition')
def get_dataset_definition():
    """
    Get dataset metadata
    Loads the metadata for a dataset specified by the 'file' relative path argument
    ---
      parameters:
        - name: file
          in: query
          description: The path to the dataset file that is to be loaded
          required: true
          type: string
          defaultValue: derived/utrecht_1829_clean_01.csv
      tags:
        - Dataset
      responses:
        '200':
          description: Dataset metadata retrieved
          schema:
            id: DatasetSchema
            type: object
            properties:
              name:
                type: string
                description: The name of the dataset
              path:
                type: string
                description: The location of the dataset on disk (server side)
              variables:
                description: A dictionary of variable names and values occurring in the dataset
                type: object
                properties:
                    default:
                        description: The default uri for this variable name
                        type: string
                    uri:
                        description: The assigned uri for this variable name
                        type: string
                    label:
                        description: The label for this variable name (i.e. the name itself)
                        type: string
                    description:
                        description: The description of the variable
                        type: string
                    category:
                        description: The category of the variable (coded, identifier, other, community)
                        type: string
                    type:
                        description: The DataCube type of the variable (sdmx:DimensionProperty, ... etc.)
                        type: string
                    codelist:
                        description: If appliccable, the codelist for this variable
                        type: object
                        properties:
                            default:
                                description: The default URI for the codelist
                                type: string
                            uri:
                                description: The assigned URI for the codelist
                                type: string
                            label:
                                description: The label for the codelist
                                type: string
                    values:
                        description: An array with values and frequencies for this variable name
                        additionalProperties:
                            description: The values and frequencies for this variable
                            schema:
                                type: object
                                items:
                                    type: object
                                    default:
                                        description: The default URI representation for this value
                                        type: string
                                    uri:
                                        description: The assigned URI representation for this value
                                        type: string
                                    label:
                                        description: The value as a label
                                        type: string
                                    literal:
                                        description: The assigned Literal representation for this value
                                    count:
                                        type: integer
                                        format: int32
            required:
                - name
                - path
                - mappings
                - values
        default:
          description: Unexpected error
          schema:
            $ref: "#/definitions/Message"
    """
    dataset_file = request.args.get('file', False)

    # Check whether a file has been provided
    if not dataset_file:
        raise(Exception('You should provide me with a relative path to the file you want to load'))


    # Create an absolute path
    dataset_path = os.path.join(config.base_path, dataset_file)
    log.debug('Dataset path: ' + dataset_path)

    cached_dataset = read_cache(dataset_path)

    if cached_dataset != {}:
        log.info("Returning from cache")
        return jsonify(cached_dataset)
    else:
        log.info("Building new dataset dictionary")

        # Specify the dataset's details
        # TODO: this is hardcoded, and needs to be gleaned from the dataset file metadata
        dataset = {
            'filename': dataset_path,
            'format': 'CSV',
            'header': True
        }

        log.debug("Initializing adapter for dataset")
        # Intialize a file a dapter for the dataset
        adapter = loader.adapter.get_adapter(dataset)


        log.debug("Preparing dataset definition response")
        # Prepare the data dictionary
        dataset_definition_response = {
            'name': adapter.get_dataset_name(),
            'uri': adapter.get_dataset_uri(),
            'file': dataset_file,
            'variables': adapter.get_values(),
        }

        return jsonify(dataset_definition_response)


@app.route('/community/dimensions')
def get_community_dimensions():
    """
    Get a list of known community-defined dimensions
    Retrieves the dimensions gathered through the LSD dimensions website
    ---
    tags:
        - Community
    responses:
        '200':
            description: Community dimensions retrieved
            schema:
                id: CommunityDimensions
                type: object
                properties:
                    dimensions:
                        description: An array of specifications as provided by LSD
                        type: array
                        items:
                            type: object
                            properties:
                                id:
                                    description: The internal ID provided by LSD
                                    type: integer
                                    format: int32
                                label:
                                    description: The name of the dimension variable
                                    type: string
                                refs:
                                    description: The number of uses of the dimension in the LOD cloud
                                    type: integer
                                    format: int32
                                uri:
                                    description: The URI of the variable
                                    type: string
                                view:
                                    descrription: Some HTML for rendering the variable (ugly leftover, ignored)
                                    type: string
                            required:
                                - label
                                - refs
                                - uri
                required:
                    - dimensions
    default:
        description: Unexpected error
        schema:
          $ref: "#/definitions/Message"
    """
    dimensions_response = {'dimensions': get_dimensions()}
    return jsonify(dimensions_response)


@app.route('/community/schemes')
def get_community_schemes():
    """
    Get a list of known community-defined concept schemes
    Retrieves concept schemes from the LOD Cloud and the CSDH
    ---
    tags:
        - Community
    responses:
        '200':
            description: Community concept schemes retrieved
            schema:
                type: object
                properties:
                    schemes:
                        description: An array of concept scheme labels and URIs
                        schema:
                            type: array
                            items:
                                description: An object specifying the label and URI of a concept scheme
                                type: object
                                properties:
                                    label:
                                        description: The name of the concept scheme
                                        type: string
                                    uri:
                                        description: The URI of the concept scheme
                                        type: string
                                required:
                                    - label
                                    - uri
                required:
                    - schemes
        default:
            description: Unexpected error
            schema:
              $ref: "#/definitions/Message"
    """
    schemes_response = {'schemes': get_schemes() + get_csdh_schemes()}
    return jsonify(schemes_response)

def read_cache(dataset_path):
    dataset_cache_filename = "{}.cache.json".format(dataset_path)

    if os.path.exists(dataset_cache_filename):
        with open(dataset_cache_filename, 'r') as dataset_cache_file:
            dataset_cache = json.load(dataset_cache_file)

        return dataset_cache
    else :
        return {}


def write_cache(dataset_path, data):
    dataset_cache_filename = "{}.cache.json".format(dataset_path)

    with open(dataset_cache_filename, 'w') as dataset_cache_file:
        json.dump(data, dataset_cache_file)


@app.route('/community/definition', methods=['GET'])
def get_community_definition():
    """
    Get the SDMX variable definition from the Web, LOD Cloud or CSDH if available
    First checks whether we already know the variable, otherwise resolves the URI
    of the variable as a URL, and retrieves its definition.
    ---
    tags:
        - Community
    parameters:
        - name: uri
          in: query
          description: The URI of the variable for which the definition is to be retrieved
          required: true
          type: string
          defaultValue: http://purl.org/linked-data/sdmx/2009/dimension#sex
    responses:
        '200':
            description: The variable definition was returned succesfully
            schema:
                type: object
                properties:
                    definition:
                        description: A variable definition
                        schema:
                            type: object
                            properties:
                                label:
                                    description: The name of the variable
                                    type: string
                                uri:
                                    description: The URI of the variable
                                    type: string
                                type:
                                    description: The DataCube type for the variable
                                    type: string
                                description:
                                    description: A description of the variable, if available
                                    type: string
                                codelist:
                                    description: An optional reference to a codelist URI for the variable
                                    schema:
                                        type: object
                                        properties:
                                            label:
                                                description: The name of the codelist
                                                type: string
                                            uri:
                                                description: The URI of the codelist
                                                type: string
                            required:
                                - uri
                required:
                    - definition
        default:
            description: Unexpected error
            schema:
              $ref: "#/definitions/Message"
    """
    uri = request.args.get('uri', False)

    if uri:
        exists = sc.ask(uri, template="""
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>

            ASK {{<{}> rdfs:label ?l .}}""")

        if not exists:
            success, visited = sc.resolve(uri, depth=2)
            print "Resolved ", visited
        else:
            success = True

        if success:
            query = """
                PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
                PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
                PREFIX dct: <http://purl.org/dc/terms/>
                PREFIX qb: <http://purl.org/linked-data/cube#>

                SELECT (<{URI}> as ?uri) ?type ?label ?description ?concept_uri WHERE {{
                    OPTIONAL
                    {{
                        <{URI}>   rdfs:label ?label .
                    }}
                    OPTIONAL
                    {{
                        <{URI}>   rdfs:comment ?description .
                    }}
                    OPTIONAL
                    {{
                        <{URI}>   a  qb:DimensionProperty .
                        BIND(qb:DimensionProperty AS ?type )
                    }}
                    OPTIONAL
                    {{
                        <{URI}>   qb:concept  ?measured_concept .
                    }}
                    OPTIONAL
                    {{
                        <{URI}>   a  qb:MeasureProperty .
                        BIND(qb:MeasureProperty AS ?type )
                    }}
                    OPTIONAL
                    {{
                        <{URI}>   a  qb:AttributeProperty .
                        BIND(qb:AttributeProperty AS ?type )
                    }}
                }}

            """.format(URI=uri)

            results = sc.sparql(query)

            log.debug(results)

            # Turn into something more manageable, and take only the first element.
            variable_definition = sc.dictize(results)[0]

            query = """
                PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
                PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
                PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
                PREFIX dct: <http://purl.org/dc/terms/>
                PREFIX qb: <http://purl.org/linked-data/cube#>

                SELECT DISTINCT ?uri ?label WHERE {{
                      <{URI}>   a               qb:CodedProperty .
                      BIND(qb:DimensionProperty AS ?type )
                      <{URI}>   qb:codeList     ?uri .
                      ?uri       rdfs:label      ?label .
                }}""".format(URI=uri)

            codelist_results = sc.sparql(query)

            log.debug(codelist_results)

            if len(codelist_results)>0:
                codelist = sc.dictize(codelist_results)
                log.debug(codelist)
                # Only take the first result (won't allow multiple code lists)
                # TODO: Check how this potentially interacts with user-added codes and lists
                variable_definition['codelist'] = codelist[0]
            else:
                log.debug("No codelist for this variable")


            log.debug("Definition for: {}".format(uri))
            log.debug(variable_definition)
            return jsonify({'definition': variable_definition})

        else:
            raise(Exception("Could not find the definition for <{}> online, nor in the CSDH".format(uri)))

    else:
        raise(Exception("No `uri` parameter given"))


@app.route('/community/concepts', methods=['GET'])
def codelist():
    """
    Get the list of concepts belonging to the code list
    Gets the SKOS Concepts belonging to the SKOS Scheme or Collection identified by the URI parameter
    ---
    tags:
        - Community
    parameters:
        - name: uri
          in: query
          description: The URI of the codelist for which the concepts are to be retrieved
          required: true
          type: string
          defaultValue: http://purl.org/linked-data/sdmx/2009/code#sex
    responses:
        '200':
            description: The codes were retrieved succesfully
            schema:
                type: object
                properties:
                    concepts:
                        description: A list of concepts belonging to the codelist
                        schema:
                            type: array
                            items:
                                description: A concept definition
                                schema:
                                    type: object
                                    properties:
                                        label:
                                            description: The preferred label of the concept
                                            type: string
                                        uri:
                                            description: The URI of the concept
                                            type: string
                                        notation:
                                            description: An optional (shorthand) notation of the concept
                                            type: string
                                    required:
                                        - label
                                        - uri
                required:
                    - concepts
        default:
            description: Unexpected error
            schema:
              $ref: "#/definitions/Message"
    """
    uri = request.args.get('uri', False)
    log.debug('Retrieving concepts for '+ uri)

    if uri:
        log.debug("Querying for SKOS concepts in Scheme or Collection <{}>".format(uri))

        query = """
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
            PREFIX dct: <http://purl.org/dc/terms/>

            SELECT DISTINCT ?uri ?label ?notation WHERE {{
              {{ ?uri skos:inScheme <{URI}> . }}
              UNION
              {{ <{URI}> skos:member+ ?uri . }}
              ?uri skos:prefLabel ?label .
              OPTIONAL {{ ?uri skos:notation ?notation . }}
            }}
        """.format(URI=uri)

        lod_codelist = []
        sdh_codelist = []

        try:
            log.debug("Querying the LOD cloud cache")
            # First we go to the LOD cloud
            sparql = SPARQLWrapper('http://lod.openlinksw.com/sparql')
            sparql.setTimeout(1)
            sparql.setReturnFormat(JSON)
            sparql.setQuery(query)

            lod_codelist_results = sparql.query().convert()['results']['bindings']
            if len(lod_codelist_results) > 0:
                lod_codelist = sc.dictize(lod_codelist_results)
            else:
                lod_codelist = []

            log.debug(lod_codelist)
        except Exception as e:
            log.error(e)
            log.error('Could not retrieve anything from the LOD cloud')
            lod_codelist = []

        try:
            log.debug("Querying the SDH")
            # Then we have a look locally
            sdh_codelist_results = sc.sparql(query)
            if len(sdh_codelist_results) > 0:
                sdh_codelist = sc.dictize(sdh_codelist_results)
            else:
                sdh_codelist = []

            log.debug(sdh_codelist)

        except Exception as e:
            log.error(e)
            log.error('Could not retrieve anything from the SDH')
            sdh_codelist = []

        if lod_codelist == [] and sdh_codelist == []:
            raise(Exception("Could not retrieve anything from LOD or CSDH"))
        else:
            return jsonify({'concepts': lod_codelist + sdh_codelist})
    else:
        raise(Exception("Missing required parameter: `uri`"))


@app.route('/dataset/save', methods=['POST'])
def dataset_save():
    """
    Save the dataset to the CSDH file cache
    Note that this does not convert the dataset to RDF, nor does it upload it to the CSDH repository
    ---
    tags:
        - Dataset
    parameters:
        - name: dataset
          in: body
          description: The dataset definition that is to be saved to cache.
          required: true
          type: object
          schema:
            $ref: "#/definitions/DatasetSchema"
    responses:
        '200':
            description: The dataset was succesfully saved to the file cache
            schema:
                $ref: "#/definitions/Message"
        default:
            description: Unexpected error
            schema:
              $ref: "#/definitions/Message"
    """
    req_json = request.get_json(force=True)

    dataset = req_json['dataset']
    dataset_path = dataset_path = os.path.join(config.base_path, dataset['file'])

    try:
        write_cache(dataset_path, dataset)
        return jsonify({'code': 200, 'message': 'Success'})
    except Exception as e:
        raise(e)


@app.route('/dataset/submit', methods=['POST'])
def dataset_submit():
    """
    Submit the dataset definition to the CSDH
    Uses the DataCube converter to convert the JSON representation of variables to RDF DataCube and commits
    the resulting RDF to the CSDH repository
    ---
    tags:
        - Dataset
    parameters:
        - name: dataset
          in: body
          description: The dataset definition that is to be converted and committed to the CSDH repository
          required: true
          schema:
            type: object
            properties:
                dataset:
                    description: The dataset definition
                    $ref: "#/definitions/DatasetSchema"
                user:
                    description: The Google user profile of the person uploading the dataset
                    type: object
    responses:
        '200':
            description: The dataset was converted succesfully
            schema:
                $ref: "#/definitions/Message"
        default:
            description: Unexpected error
            schema:
              $ref: "#/definitions/Message"
    """

    req_json = request.get_json(force=True)
    dataset = req_json['dataset']
    user = req_json['user']
    source_hash = git_client.add_file(dataset['file'], user['name'], user['email'])
    log.debug("Using {} as dataset hash".format(source_hash))

    rdf_dataset = datacube.converter.data_structure_definition(user, dataset['name'], dataset['uri'], dataset['variables'], dataset['file'], source_hash)

    # data = util.inspector.update(dataset)
    # socketio.emit('update', {'data': data}, namespace='/inspector')

    trig = datacube.converter.serializeTrig(rdf_dataset)

    with open('latest_update.trig', 'w') as f:
        f.write(trig)

    for graph in rdf_dataset.contexts():
        graph_uri = graph.identifier
        sc.post_data(graph.serialize(format='turtle'), graph_uri=graph_uri)

    return jsonify({'code': 200, 'message': 'Succesfully submitted datastructure definition to CSDH'})


@app.route('/browse', methods=['GET'])
def browse():
    """
    Browse the dataset file cache
    Takes a relative path, and returns a list of files/directories at that location as JSON
    ---
      tags:
        - Base
      parameters:
        - name: path
          in: query
          description: The relative path to be browsed
          required: true
          type: string
          defaultValue: .
      responses:
        '200':
          description: Path retrieved
          schema:
            description: A path specification
            type: object
            properties:
                path:
                    description: The current path
                    type: string
                parent:
                    description: The parent path
                    type: string
                files:
                    description: The list of files found at this location
                    type: array
                    items:
                        description: A file, its path, name and its mimetype
                        schema:
                            id: FileInfo
                            type: object
                            properties:
                                label:
                                    description: The name of the file
                                    type: string
                                mimetype:
                                    description: The guessed mimetype of the file (libmagic)
                                    type: string
                                type:
                                    description: Whether it is a directory (dir) or normal file (file)
                                    type: string
                                uri:
                                    description: The relative path of the file
                                    type: string
                            required:
                                - label
                                - mimetype
                                - type
                                - uri
            required:
                - path
                - parent
                - files
        default:
          description: Unexpected error
          schema:
            id: Message
            type: object
            properties:
              code:
                type: integer
                format: int32
              message:
                type: string
    """
    path = request.args.get('path', None)

    if not path:
        raise Exception('Must specify a path!')

    log.debug('Will browse absolute path: {}/{}'.format(config.base_path, path))
    filelist, parent = fc.browse(config.base_path, path)

    return jsonify({'path': path, 'parent': parent, 'files': filelist})


@app.route('/iri', methods=['GET'])
def iri():
    """
    Bake an IRI using iribaker
    Checks an IRI for compliance with RFC and converts invalid characters to underscores, if possible.
    **NB**: No roundtripping, this procedure may result in identity smushing: two input-IRI's may be
    mapped to the same output-IRI.
    ---
      tags:
        - Base
      consumes:
        - text/json
      parameters:
        - name: iri
          in: query
          description: The IRI to be checked for compliance
          required: true
          type: string
      responses:
        '200':
          description: IRI converted
          schema:
            description: A converted IRI result
            type: object
            properties:
                iri:
                    description: The fully compliant IRI
                    type: string
                source:
                    description: The input IRI
                    type: string
            required:
                - iri
                - source
        default:
          description: Unexpected error
          schema:
            id: Message
            type: object
            properties:
              code:
                type: integer
                format: int32
              message:
                type: string
    """

    unsafe_iri = request.args.get('iri', None)

    if unsafe_iri is not None:
        response = {'iri': iribaker.to_iri(unsafe_iri), 'source': unsafe_iri}
        return jsonify(response)
    else:
        raise(Exception("The IRI {} could not be converted to a compliant IRI".format(unsafe_iri)))


def get_dimensions():
    # Get the LSD dimensions from the LSD service (or a locally cached copy)
    # And concatenate it with the dimensions in the CSDH
    # Return an ordered dict of dimensions (ordered by number of references)

    dimensions = get_lsd_dimensions() + get_csdh_dimensions()

    # dimensions_as_dict = {dim['uri']: dim for dim in dimensions}
    sorted_dimensions = sorted(dimensions, key=lambda t: t['refs'])

    # sorted_dimensions = OrderedDict(sorted(dimensions_as_dict.items(), key=lambda t: t[1]['refs']))
    return sorted_dimensions


def get_lsd_dimensions():
    """Loads the list of Linked Statistical Data dimensions (variables) from the LSD portal"""
    # TODO: Create a local copy that gets updated periodically

    try:
        if os.path.exists('metadata/dimensions.json'):
            log.debug("Loading dimensions from file...")
            with open('metadata/dimensions.json', 'r') as f:
                dimensions_json = f.read()
            log.debug("Dimensions loaded...")
            dimensions = json.loads(dimensions_json)
        else:
            raise Exception("Could not load dimensions from file...")
    except Exception as e:
        log.warning(e)
        dimensions_response = requests.get("http://amp.ops.few.vu.nl/data.json")
        log.debug("Loading dimensions from LSD service...")
        try:
            dimensions = json.loads(dimensions_response.content)

            if len(dimensions_response) > 1:
                with open('metadata/dimensions.json', 'w') as f:
                    f.write(dimensions_response)
            else:
                raise Exception("Could not load dimensions from service")

        except Exception as e:
            log.error(e)

            dimensions = []

    dimensions = [dim for dim in dimensions if dim['refs'] > 1]
    return dimensions


def get_csdh_dimensions():
    """Loads the list of Linked Statistical Data dimensions (variables) from the CSDH"""
    log.debug("Loading dimensions from the CSDH")
    query = """
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        PREFIX dct: <http://purl.org/dc/terms/>
        PREFIX qb: <http://purl.org/linked-data/cube#>

        SELECT DISTINCT ?uri ?label ("CSDH" as ?refs) WHERE {
          {
              ?uri a qb:DimensionProperty .
              ?uri rdfs:label ?label .
          }
          UNION
          {
              ?uri a qb:MeasureProperty .
              ?uri rdfs:label ?label .
          }
          UNION
          {
              ?uri a qb:AttributeProperty .
              ?uri rdfs:label ?label .
          }
        }
    """
    sdh_dimensions_results = sc.sparql(query)
    try :
        if len(sdh_dimensions_results) > 0:
            sdh_dimensions = sc.dictize(sdh_dimensions_results)
        else:
            sdh_dimensions = []
    except Exception as e:
        log.error(e)
        sdh_dimensions = []

    return sdh_dimensions


def get_csdh_schemes():
    """Loads SKOS Schemes (code lists) from the CSDH"""
    log.debug("Querying CSDH Cloud")

    query = """
        PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        PREFIX dct: <http://purl.org/dc/terms/>

        SELECT DISTINCT ?uri ?label WHERE {
          {
              ?c skos:inScheme ?uri .
              ?uri rdfs:label ?label .
          }
          UNION
          {
              ?uri skos:member ?c .
              ?uri rdfs:label ?label .
          }

        }
    """

    schemes_results = sc.sparql(query)
    log.debug(schemes_results)
    schemes = sc.dictize(schemes_results)

    log.debug(schemes)

    return schemes


def get_schemes():
    """Loads SKOS Schemes (code lists) either from the LOD Cache, or from a cached copy"""
    if os.path.exists('metadata/schemes.json'):
        # TODO: Check the age of this file, and update if older than e.g. a week.
        log.debug("Loading schemes from file...")
        with open('metadata/schemes.json', 'r') as f:
            schemes_json = f.read()

        schemes = json.loads(schemes_json)
        return schemes
    else:
        log.debug("Loading schemes from RDF sources...")
        schemes = []

        ### ---
        ### Querying the LOD Cloud
        ### ---
        log.debug("Querying LOD Cloud")

        query = """
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
            PREFIX dct: <http://purl.org/dc/terms/>

            SELECT DISTINCT ?scheme ?label WHERE {
              ?c skos:inScheme ?scheme .
              ?scheme rdfs:label ?label .
            }
        """

        sparql = SPARQLWrapper('http://lod.openlinksw.com/sparql')
        sparql.setReturnFormat(JSON)
        sparql.setQuery(query)

        results = sparql.query().convert()

        for r in results['results']['bindings']:
            scheme = {}

            scheme['label'] = r['label']['value']
            scheme['uri'] = r['scheme']['value']
            schemes.append(scheme)

        log.debug("Found {} schemes".format(len(schemes)))
        ### ---
        ### Querying the HISCO RDF Specification (will become a call to a generic CLARIAH Vocabulary Portal thing.)
        ### ---
        log.debug("Querying HISCO RDF Specification")

        query = """
            PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
            PREFIX dct: <http://purl.org/dc/terms/>

            SELECT DISTINCT ?scheme ?label WHERE {
              ?scheme a skos:ConceptScheme.
              ?scheme dct:title ?label .
            }
        """

        g = Graph()
        g.parse('metadata/hisco.ttl', format='turtle')

        results = g.query(query)

        for r in results:
            scheme = {}
            scheme['label'] = r.label
            scheme['uri'] = r.scheme
            schemes.append(scheme)

        log.debug("Found a total of {} schemes".format(len(schemes)))

        schemes_json = json.dumps(schemes)

        with open('metadata/schemes.json', 'w') as f:
            f.write(schemes_json)

        return schemes


@app.after_request
def after_request(response):
    """
    Needed for Swagger UI
    """
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', "Authorization, Content-Type")
    response.headers.add('Access-Control-Expose-Headers', "Authorization")
    response.headers.add('Access-Control-Allow-Methods', "GET, POST, PUT, DELETE, OPTIONS")
    response.headers.add('Access-Control-Allow-Credentials', "true")
    response.headers.add('Access-Control-Max-Age', 60 * 60 * 24 * 20)
    return response
