var QBerDispatcher = require('../dispatcher/QBerDispatcher');
var QBerAPI = require('../utils/QBerAPI');
var SDMXDimensionConstants = require('../constants/SDMXDimensionConstants');
var DatasetConstants = require('../constants/DatasetConstants');
var MessageConstants = require('../constants/MessageConstants');


var SDMXDimensionActions = {


  /**
   * @param {string} dimension
   */
  chooseDimension: function(dimension) {
    console.log("You chose dimension: "+dimension);
    QBerDispatcher.dispatch({
      actionType: MessageConstants.INFO,
      message: 'Retrieving details for '+dimension
    });

    this.hideDimensions();

    QBerAPI.retrieveDimension({
      dimension: dimension,
      success: function(dimension_details){
        QBerDispatcher.dispatch({
          actionType: MessageConstants.SUCCESS,
          message: "Successfully retrieved dimension "+ dimension
        });
        QBerDispatcher.dispatch({
          actionType: SDMXDimensionConstants.SDMX_DIMENSION_ASSIGN,
          dimension_details: dimension_details
        });
      },
      error: function(dimension){
        QBerDispatcher.dispatch({
          actionType: MessageConstants.ERROR,
          message: "Could not retrieve dimension "+ dimension
        });

        // Dispatch an 'empty' dimension_details object
        var dimension_details = {'uri': dimension};
        QBerDispatcher.dispatch({
          actionType: SDMXDimensionConstants.SDMX_DIMENSION_ASSIGN,
          dimension_details: dimension_details
        });
      }
    });
  },

  /**
   * Updated dimension details, assign them to the current variable.
   */
  updateDimension: function(dimension_details) {
    QBerDispatcher.dispatch({
      actionType: SDMXDimensionConstants.SDMX_DIMENSION_ASSIGN,
      dimension_details: dimension_details
    });
  },


  /**
   *
   */
  showDimensions: function() {
    QBerDispatcher.dispatch({
      actionType: SDMXDimensionConstants.SDMX_DIMENSION_SHOW,
    });
  },

  /**
   *
   */
  hideDimensions: function() {
    QBerDispatcher.dispatch({
      actionType: SDMXDimensionConstants.SDMX_DIMENSION_HIDE,
    });
  },

  /**
   * @param {string} iri
   */
  retrieveIRI: function(unsafe_iri){
    console.log('Retrieving safe IRI based on '+unsafe_iri);
    QBerDispatcher.dispatch({
      actionType: MessageConstants.INFO,
      message: 'Retrieving safe IRI based on '+unsafe_iri
    });
    // Call the QBerAPI with the filename, and implement the success callback
    QBerAPI.retrieveIRI({
      iri: unsafe_iri,
      success: function(iri){
        QBerDispatcher.dispatch({
          actionType: MessageConstants.SUCCESS,
          message: 'Successfully minted fresh IRI '+iri
        });

        QBerDispatcher.dispatch({
          actionType: SDMXDimensionConstants.SDMX_DIMENSION_UPDATE_IRI,
          iri: iri
        });
      },
      error: function(unsafe_iri){
        QBerDispatcher.dispatch({
          actionType: MessageConstants.ERROR,
          message: 'Could not generate safe IRI from '+filename
        });
      }
    });
  },


};

module.exports = SDMXDimensionActions;
