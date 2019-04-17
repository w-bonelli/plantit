import axios from 'axios'

function pathJoin(parts){
  /**
   * Joins names into a valid file path
   *
   * Args:
   *   parts (Array of strings): Names to join into a path.
   *
   * Returns:
   *   A string containing each element in parts with a single / between them.
   *   extra /s in the elements of parts are removed
   **/
   var replace   = new RegExp('/'+'{1,}', 'g');
   return parts.join('/').replace(replace, '/');
}

export default {
   getStorageTypes() {
    /**
     * Get Available stroage types.
     *
     * Returns:
     *    Axios promise containing returning an array of storage types
     **/
    return axios.post("/apis/v1/files/")
    .then((response) => {return response.data.storage_types})
    .catch(function (error) {
      console.log("Error: " + error);
    })
  },

  listDir(dir,storage_type) {
    /**
     * List folder contents in the format required by
     * jsTree  (https://www.jstree.com/docs/json/)
     *
     * Requirements:
     *   User must be logged in
     *   User must have permission to access dir
     *
     * Args:
     *    dir (str): path of directory to list,
     *    storage_type (str): The storage system to access
     **/
     return axios.get(`/apis/v1/files/lsdir/`,{
       params:{
         'path': dir,
         'storage_type': storage_type
       }
     }).then((response) => {return response.data})
     .catch((error) => {console.log(error)})
  },

  listDirBase(basePath, dir, storage_type) {
    /**
     * List folder contents in the format required by
     * jsTree  (https://www.jstree.com/docs/json/)
     *
     * Similar to listDir, except basePath and dir are combined
     * for the api call, then basePath is removed from the item.path
     * before it is returned.
     *
     * Requirements:
     *   User must be logged in
     *   User must have permission to access dir
     *
     * Args:
     *    basePath (str): The base path
     *    dir (str): path of directory to list,
     *    storage_type (str): The storage system to access
     **/
     console.log(pathJoin([basePath,dir]))
     return axios.get(`/apis/v1/files/lsdir/`,{
       params:{
         'path': pathJoin([basePath,dir]),
         'storage_type': storage_type
       }
     }).then((response) => {
       return response.data.map((item) => {
         item.path = item.path.replace(basePath,'')
         return item
       })
     })
     .catch((error) => {console.log(error)})
  }


}
